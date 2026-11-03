#!/usr/bin/env python3
"""Mirror the combined paper account onto an OKX *demo trading* account (learning only).

The paper trader (tools/paper_trader.py) decides; this script executes those decisions on OKX's demo
environment so that real order handling, fills, slippage and exchange-side stops can be compared with
the paper results. Every request carries `x-simulated-trading: 1`; it cannot reach a real account.

Each run, against <data>/combined/state.json:
  - a paper position opened in the last OPEN_WINDOW that is not mirrored yet is opened at market,
    isolated margin, sized to 1% of the demo account's equity at the paper stop, with the stop (and the
    mean-reversion target) attached as exchange-side algo orders;
  - when the paper stop trails, the exchange stop is amended;
  - when the paper position is gone, the mirrored position is closed;
  - if the exchange stop already closed a mirrored position, it is marked done and never reopened.
Safety: a new mirror starts paused (check with `dry-run`, then `resume`); positions not opened by this script are never touched; leverage is capped at 10x; a UTC+7 day
loss of 8% closes everything and blocks new entries until the next day; actions `pause`, `resume`,
`flatten` (close all and pause) and `dry-run` (log intended orders, send nothing) are available.
Secrets come from OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE, reach curl through stdin and are never logged.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from volume_test import profile as volume_profile, describe as describe_volume

BASE = 'https://www.okx.com'
INSTRUMENTS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP']
RISK, MAX_LEVERAGE, DAILY_HALT = .01, 10, .08
HISTORY_POINTS = 3000            # equity curve points kept (~31 days at one point per 15 minutes)
HISTORY_STEP = 15 * 60_000       # runs every 5 minutes; keep one curve point per 15 minutes
OPEN_WINDOW = 2 * 3600_000        # only mirror paper entries that are at most this old
MIN_STOP_DIST = .002              # skip entries whose stop is already too close to the current price
AMEND_TOLERANCE = .001            # amend the exchange stop when the paper stop moved more than 0.1%
UTC7 = timezone(timedelta(hours=7))
TAG = {'mean_reversion': 'mr', 'breakout_long': 'bl', 'breakout_daily': 'bd'}


def configure_volume_test(now=None):
    global INSTRUMENTS, RISK
    p = volume_profile(now)
    INSTRUMENTS, RISK = p['instruments'], p['risk']
    return p


class OKXError(RuntimeError):
    pass


class OKX:
    """Minimal signed client for OKX demo trading."""
    def __init__(self, key, secret, passphrase, curl='curl', attempts=3):
        self.key, self.secret, self.passphrase, self.curl = key, secret, passphrase, curl
        self.attempts = attempts

    def request(self, method, path, body=None):
        payload = json.dumps(body, separators=(',', ':')) if body is not None else ''
        ts = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        sign = base64.b64encode(hmac.new(self.secret.encode(), f'{ts}{method}{path}{payload}'.encode(), hashlib.sha256).digest()).decode()
        headers = {'OK-ACCESS-KEY': self.key, 'OK-ACCESS-SIGN': sign, 'OK-ACCESS-TIMESTAMP': ts, 'OK-ACCESS-PASSPHRASE': self.passphrase,
                   'x-simulated-trading': '1', 'Content-Type': 'application/json'}
        config = ''.join(f'header = "{k}: {v}"\n' for k, v in headers.items()) + f'url = "{BASE}{path}"\nrequest = "{method}"\n'
        if payload:
            config += 'data = "' + payload.replace('\\', '\\\\').replace('"', '\\"') + '"\n'
        for attempt in range(self.attempts):
            out = subprocess.run([self.curl, '-sS', '-m', '20', '-K', '-'], input=config, capture_output=True, text=True)
            if out.returncode == 0:
                try:
                    return json.loads(out.stdout)
                except ValueError:
                    pass
            if attempt + 1 < self.attempts:
                time.sleep(1 + attempt)
        raise OKXError(f'{method} {path.split("?")[0]} failed')


def ok(resp):
    return resp.get('code') == '0'


def first_error(resp):
    d = (resp.get('data') or [{}])[0] if isinstance(resp.get('data'), list) else {}
    return f"{resp.get('code')} {resp.get('msg', '')} {d.get('sCode', '')} {d.get('sMsg', '')}".strip()


def quantize(value, step, rounding=ROUND_DOWN):
    step = Decimal(str(step))
    return (Decimal(str(value)) / step).to_integral_value(rounding) * step


def fmt(d):
    s = format(d, 'f')
    return s.rstrip('0').rstrip('.') if '.' in s else s


def local_day(now):
    return now.astimezone(UTC7).date().isoformat()


def new_state():
    # Starts paused: run `dry-run` first, then `resume` to begin placing demo orders.
    return dict(mirrored={}, paused=True, halted=False, day=None, day_start=None)


def dashboard_actions(out, actions):
    """Join confirmed execution-test fills with strategy actions without replaying orders."""
    rows, closed = list(actions), set()
    for path in sorted(out.glob('smoke-test-*.json')):
        report = json.loads(path.read_text())
        if report.get('demo') is not True:
            continue
        entry, exit_order = report.get('entry') or {}, report.get('exit') or {}
        if entry.get('state') != 'filled' or not entry.get('ordId'):
            continue
        for kind, fill in [('open', entry), ('close', exit_order)]:
            if fill.get('state') != 'filled' or not fill.get('ordId'):
                continue
            ts = datetime.fromtimestamp(int(fill['fillTime']) / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            action = dict(ts=ts, action=kind, inst=report['instrument'], strategy='execution_test', execution_test=True,
                          dry_run=False, side=1, contracts=fill['accFillSz'], price=float(fill['avgPx']),
                          leverage=int(fill['lever']), order_id=fill['ordId'])
            if kind == 'open':
                action['stop'] = report.get('stop')
            else:
                closed.add(entry['ordId'])
                action.update(reason='execution_test', pnl=float(fill.get('pnl') or 0),
                              fee=abs(float(entry.get('fee') or 0)) + abs(float(fill.get('fee') or 0)))
                action['net_pnl'] = action['pnl'] - action['fee']
            rows.append(action)
    unique = {}
    for a in rows:
        key = a.get('order_id') or (a.get('ts'), a.get('action'), a.get('inst'))
        unique[key] = a
    return sorted(unique.values(), key=lambda a: a['ts'], reverse=True)[:30], len(closed)


class Mirror:
    def __init__(self, client, state, paper, now, dry_run=False):
        self.c, self.s, self.paper, self.now, self.dry = client, state, paper, now, dry_run
        self.actions = []
        self.now_ms = int(now.timestamp() * 1000)

    def log(self, kind, inst, **kw):
        self.actions.append(dict(ts=self.now.isoformat(timespec='seconds').replace('+00:00', 'Z'), action=kind, inst=inst, dry_run=self.dry, **kw))

    def post(self, path, body):
        if self.dry:
            return {'code': '0', 'data': [{'sCode': '0', 'dry_run': True}]}
        return self.c.request('POST', path, body)

    # ---- exchange reads ------------------------------------------------------
    def load(self):
        cfg = self.c.request('GET', '/api/v5/account/config')
        if not ok(cfg):
            raise OKXError('account config: ' + first_error(cfg))
        c = cfg['data'][0]
        if str(c.get('acctLv')) == '1':
            raise OKXError('账户为现货模式，无法交易永续合约：请在 OKX 模拟交易中切换到合约模式')
        if 'withdraw' in str(c.get('perm', '')):
            raise OKXError('密钥开启了提现权限，已拒绝运行')
        self.hedged = c.get('posMode') == 'long_short_mode'
        bal = self.c.request('GET', '/api/v5/account/balance?ccy=USDT')
        usdt = next((x for x in (bal.get('data') or [{}])[0].get('details', []) if x.get('ccy') == 'USDT'), {}) if ok(bal) else {}
        self.equity = float(usdt.get('eq') or 0)
        self.available = float(usdt.get('availBal') or 0)
        pos = self.c.request('GET', '/api/v5/account/positions?instType=SWAP')
        self.positions = {}
        managed = set(INSTRUMENTS) | {m['inst'] for m in self.s['mirrored'].values() if m.get('status') == 'open'}
        for p in (pos.get('data') or []) if ok(pos) else []:
            if p.get('instId') in managed and float(p.get('pos') or 0) != 0:
                self.positions[(p['instId'], p.get('posSide', 'net'))] = p
        algos = self.c.request('GET', '/api/v5/trade/orders-algo-pending?instType=SWAP&ordType=conditional')
        oco = self.c.request('GET', '/api/v5/trade/orders-algo-pending?instType=SWAP&ordType=oco')
        self.algos = [a for r in (algos, oco) if ok(r) for a in r.get('data') or [] if a.get('instId') in managed]
        self.specs = {}

    def spec(self, inst):
        if inst not in self.specs:
            r = self.c.request('GET', f'/api/v5/public/instruments?instType=SWAP&instId={inst}')
            d = (r.get('data') or [{}])[0]
            self.specs[inst] = dict(ctVal=float(d.get('ctVal') or 0), lotSz=d.get('lotSz') or '1', minSz=float(d.get('minSz') or 0), tickSz=d.get('tickSz') or '0.0001')
        return self.specs[inst]

    def price(self, inst):
        r = self.c.request('GET', f'/api/v5/market/ticker?instId={inst}')
        return float((r.get('data') or [{}])[0].get('last') or 0) if ok(r) else 0.0

    def held(self, inst, side):
        ps = 'long' if side == 1 else 'short'
        return self.positions.get((inst, ps)) or self.positions.get((inst, 'net'))

    # ---- actions ---------------------------------------------------------------
    def open(self, key, inst, p):
        px = self.price(inst)
        side = p['side']
        if not px:
            return self.log('skip', inst, reason='no_price')
        dist = (px - p['stop']) / px * side
        if dist < MIN_STOP_DIST:
            return self.log('skip', inst, reason='stop_too_close_or_crossed', price=px, stop=p['stop'])
        if p.get('tp') and (p['tp'] - px) * side <= 0:
            return self.log('skip', inst, reason='target_already_reached', price=px, target=p['tp'])
        sp = self.spec(inst)
        if not sp['ctVal']:
            return self.log('skip', inst, reason='no_instrument_spec')
        risk = RISK * self.equity
        contracts = quantize(risk / (px * dist) / sp['ctVal'], sp['lotSz'])
        if float(contracts) < sp['minSz'] or contracts <= 0:
            return self.log('skip', inst, reason='size_below_minimum', contracts=fmt(contracts), min=sp['minSz'])
        lever = max(1, min(MAX_LEVERAGE, int(p.get('leverage') or MAX_LEVERAGE), int(.8 / dist)))
        margin = float(contracts) * sp['ctVal'] * px / lever
        if margin > self.available * .95:
            return self.log('skip', inst, reason='insufficient_margin', margin=round(margin, 4), available=round(self.available, 4))
        pos_side = ('long' if side == 1 else 'short') if self.hedged else None
        lev_body = dict(instId=inst, lever=str(lever), mgnMode='isolated', **({'posSide': pos_side} if pos_side else {}))
        r = self.post('/api/v5/account/set-leverage', lev_body)
        if not ok(r):
            return self.log('error', inst, step='set_leverage', detail=first_error(r))
        tick = sp['tickSz']
        algo = dict(slTriggerPx=fmt(quantize(p['stop'], tick, ROUND_HALF_UP)), slOrdPx='-1', slTriggerPxType='last')
        if p.get('tp'):
            algo.update(tpTriggerPx=fmt(quantize(p['tp'], tick, ROUND_HALF_UP)), tpOrdPx='-1', tpTriggerPxType='last')
        cl = f"{TAG.get(p.get('strategy'), 'px')}{inst.split('-')[0].lower()}{int(p['opened']) // 1000}"[:32]
        order = dict(instId=inst, tdMode='isolated', side='buy' if side == 1 else 'sell', ordType='market', sz=fmt(contracts),
                     clOrdId=cl, attachAlgoOrds=[algo], **({'posSide': pos_side} if pos_side else {}))
        r = self.post('/api/v5/trade/order', order)
        if not ok(r):
            return self.log('error', inst, step='order', detail=first_error(r), size=fmt(contracts))
        self.s['mirrored'][key] = dict(inst=inst, side=side, posSide=pos_side or 'net', strategy=p.get('strategy'), contracts=fmt(contracts),
                                       stop=p['stop'], target=p.get('tp'), leverage=lever, clOrdId=cl, opened_at=self.now_ms, status='open',
                                       ordId=(r.get('data') or [{}])[0].get('ordId'))
        self.log('open', inst, side=side, strategy=p.get('strategy'), contracts=fmt(contracts), price=px, stop=algo['slTriggerPx'],
                 target=algo.get('tpTriggerPx'), leverage=lever, risk_usdt=round(risk, 2))

    def close(self, key, m, reason):
        body = dict(instId=m['inst'], mgnMode='isolated', autoCxl=True, **({'posSide': m['posSide']} if m['posSide'] != 'net' else {}))
        r = self.post('/api/v5/trade/close-position', body)
        if not ok(r):
            return self.log('error', m['inst'], step='close', detail=first_error(r))
        m['status'] = 'done'; m['closed_reason'] = reason
        for k in [k for k in self.positions if k[0] == m['inst'] and k[1] in (m['posSide'], 'net')]:
            self.positions.pop(k)  # gone now, so a fresh paper entry on this coin can be mirrored in the same run
        self.log('close', m['inst'], strategy=m.get('strategy'), reason=reason)

    def amend_stop(self, key, m, new_stop):
        algo = next((a for a in self.algos if a['instId'] == m['inst'] and (a.get('posSide', 'net') == m['posSide'] or m['posSide'] == 'net')), None)
        if not algo:
            return self.log('error', m['inst'], step='amend_stop', detail='algo order not found')
        tick = self.spec(m['inst'])['tickSz']
        r = self.post('/api/v5/trade/amend-algos', dict(instId=m['inst'], algoId=algo['algoId'], newSlTriggerPx=fmt(quantize(new_stop, tick, ROUND_HALF_UP))))
        if not ok(r):
            return self.log('error', m['inst'], step='amend_stop', detail=first_error(r))
        self.log('amend_stop', m['inst'], old=m['stop'], new=new_stop); m['stop'] = new_stop

    def flatten(self, reason):
        for key, m in self.s['mirrored'].items():
            if m['status'] == 'open' and self.held(m['inst'], m['side']):
                self.close(key, m, reason)
            elif m['status'] == 'open':
                m['status'] = 'done'

    # ---- one reconciliation pass ---------------------------------------------------
    def run(self, action='run'):
        self.load()
        day = local_day(self.now)
        if self.s.get('day') != day:
            self.s.update(day=day, day_start=self.equity, halted=False)
        if action == 'pause':
            self.s['paused'] = True; self.log('pause', '-')
        elif action == 'resume':
            self.s['paused'] = False; self.log('resume', '-')
        elif action == 'follow-current':
            # Manual catch-up: entries skipped only for being older than OPEN_WINDOW are retried at the current price;
            # the stop / target / size / margin checks in open() still apply.
            for key, m in list(self.s['mirrored'].items()):
                if m['status'] == 'skipped' and m.get('reason') == 'too_old':
                    del self.s['mirrored'][key]
            self.log('follow_current', '-')
        elif action == 'flatten':
            self.s['paused'] = True; self.flatten('manual_flatten'); self.log('flatten', '-')
            return
        if not self.s['halted'] and self.s.get('day_start') and self.equity <= self.s['day_start'] * (1 - DAILY_HALT):
            self.s['halted'] = True; self.log('daily_halt', '-', equity=self.equity, day_start=self.s['day_start'])
            self.flatten('daily_halt')
            return
        paper_positions = self.paper.get('positions', {})
        # Closes and stop updates for what we mirrored.
        for key, m in self.s['mirrored'].items():
            if m['status'] != 'open':
                continue
            held = self.held(m['inst'], m['side'])
            p = paper_positions.get(m['inst'])
            same = p and f"{m['inst']}|{int(p['opened'])}" == key
            if not held:
                m['status'] = 'done'; m['closed_reason'] = 'exchange_stop_or_target'
                self.log('closed_on_exchange', m['inst'], strategy=m.get('strategy'))
            elif not same:
                self.close(key, m, 'paper_closed')
            elif abs(p['stop'] - m['stop']) / m['stop'] > AMEND_TOLERANCE and (p['stop'] - m['stop']) * m['side'] > 0:
                self.amend_stop(key, m, p['stop'])
        # New entries (a dry run previews them as if resumed).
        if (self.s['paused'] and not self.dry) or self.s['halted']:
            return
        for inst, p in sorted(paper_positions.items()):
            key = f"{inst}|{int(p['opened'])}"
            if key in self.s['mirrored'] or inst not in INSTRUMENTS:
                continue
            if self.held(inst, p['side']) or any(k[0] == inst for k in self.positions):
                self.log('skip', inst, reason='exchange_position_exists_not_ours'); self.s['mirrored'][key] = dict(inst=inst, status='skipped', side=p['side'], posSide='net')
                continue
            if self.now_ms - int(p['opened']) > OPEN_WINDOW and action != 'follow-current':
                self.s['mirrored'][key] = dict(inst=inst, status='skipped', side=p['side'], posSide='net', reason='too_old')
                self.log('skip', inst, reason='paper_entry_too_old')
                continue
            self.open(key, inst, p)

    def summary(self):
        pos = [dict(inst=p['instId'], side=p.get('posSide', 'net'), contracts=p.get('pos'), avgPx=p.get('avgPx'), markPx=p.get('markPx'),
                    upl=p.get('upl'), uplRatio=p.get('uplRatio'), lever=p.get('lever'), margin=p.get('margin'), ctVal=self.spec(p['instId'])['ctVal'],
                    ours=any(m['status'] == 'open' and m['inst'] == p['instId'] for m in self.s['mirrored'].values()))
               for p in self.positions.values()]
        return dict(updated_at=self.now.isoformat(timespec='seconds').replace('+00:00', 'Z'), equity=self.equity, available=self.available,
                    day_start=self.s.get('day_start'), paused=self.s['paused'], halted=self.s['halted'], dry_run=self.dry, positions=pos,
                    open_mirrors=[dict(m, key=k) for k, m in self.s['mirrored'].items() if m['status'] == 'open'],
                    start_equity=self.s.get('start_equity'), history=self.curve(),
                    closed_trades=sum(1 for m in self.s['mirrored'].values() if m['status'] == 'done'))

    def record_equity(self):
        """Equity curve for the dashboard: at most one stored point per HISTORY_STEP, capped."""
        self.s.setdefault('start_equity', self.equity)
        hist = self.s.setdefault('history', [])
        if not hist or self.now_ms - hist[-1][0] >= HISTORY_STEP:
            hist.append([self.now_ms, round(self.equity, 2)])
        del hist[:-HISTORY_POINTS]

    def curve(self):
        """Stored points plus the current equity, so the curve always ends at the latest run."""
        hist = self.s.get('history', [])[-HISTORY_POINTS:]
        if hasattr(self, 'equity') and (not hist or hist[-1][0] != self.now_ms):
            hist = hist + [[self.now_ms, round(self.equity, 2)]]
        return hist


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--action', default='run', choices=['run', 'dry-run', 'pause', 'resume', 'flatten', 'follow-current', 'snapshot'])
    args = ap.parse_args()
    print(describe_volume(configure_volume_test()))
    creds = [os.environ.get(k, '').strip() for k in ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE')]
    if not all(creds):
        print('未配置 OKX 模拟交易密钥，跳过跟单。'); return
    paper_path = args.data / 'combined' / 'state.json'
    if not paper_path.exists():
        print('没有合并模拟盘状态，跳过跟单。'); return
    out = args.data / 'okx_demo'; out.mkdir(parents=True, exist_ok=True)
    state_path = out / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else new_state()
    dry = args.action == 'dry-run'
    mirror = Mirror(OKX(*creds), json.loads(json.dumps(state)) if dry else state, json.loads(paper_path.read_text()), datetime.now(timezone.utc), dry)
    error = None
    try:
        if args.action == 'snapshot':
            mirror.load()  # Read-only exchange refresh: no order, leverage or stop mutations.
        else:
            mirror.run('run' if dry else args.action)
    except OKXError as exc:
        error = str(exc); mirror.log('error', '-', step='run', detail=error)
    for a in mirror.actions:
        print(json.dumps(a, ensure_ascii=False))
    if not dry and hasattr(mirror, 'equity'):
        mirror.record_equity()
    if not dry:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True))
        with (out / 'actions.jsonl').open('a') as f:
            f.writelines(json.dumps(a, ensure_ascii=False) + '\n' for a in mirror.actions)
        if hasattr(mirror, 'equity'):
            summary = mirror.summary()
            lines = (out / 'actions.jsonl').read_text().splitlines()[-30:]
            summary['recent_actions'], tests_closed = dashboard_actions(out, [json.loads(l) for l in lines])
            summary['execution_test_trades'] = tests_closed
            summary['closed_trades'] += tests_closed
            (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    if error:
        sys.exit(1)


if __name__ == '__main__':
    main()
