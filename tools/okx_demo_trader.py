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
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from volume_test import profile as volume_profile, describe as describe_volume
from runtime_guard import entries_allowed, atomic_json

BASE = 'https://www.okx.com'
INSTRUMENTS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP']
RISK, MAX_LEVERAGE, DAILY_HALT = .01, 10, .10   # daily loss halt: 10 % below the day's starting equity
EVOLVED_DAILY_HALT = .10                         # following the evolution champions: 10 % too, counted WITHOUT the leader's result
RISK_RANGE = (.01, .05)   # evolved mode: each order risks its strategy's own risk gene, kept inside this range
# The team leader (signals with leader=True; chosen by the user): 20 % of the whole account per trade, up to 50× leverage
# (never more than the coin allows), outside the same-side cap and outside the daily halt.
LEADER_RISK, LEADER_LEVERAGE = .20, 50
SAME_SIDE_CAP = .05
HISTORY_POINTS = 3000            # equity curve points kept (~31 days at one point per 15 minutes)
HISTORY_STEP = 15 * 60_000       # runs every 5 minutes; keep one curve point per 15 minutes
OPEN_WINDOW = 5 * 3600_000        # paper entries at most this old are mirrored as they are (runs are 4 h apart, plus delay)
LATE_BAND = (.5, 1.5)             # older entries are still followed while the price is within this band of the paper stop distance
MIN_STOP_DIST = .002              # skip entries whose stop is already too close to the current price
AMEND_TOLERANCE = .001            # amend the exchange stop when the paper stop moved more than 0.1%
UTC7 = timezone(timedelta(hours=7))
TAG = {'mean_reversion': 'mr', 'breakout_long': 'bl', 'breakout_daily': 'bd', 'evolved': 'ev'}
EVOLVED_INSTRUMENTS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP']
ACTIVE_PROFILE = None


def configure_evolved(universe=None):
    """Following the evolution champion (tools/evolve): the old strategy profiles no longer apply. `universe` is the
    coin list the evolution trades (signals.json 'instruments', the top-100 perpetuals); without it, the five majors."""
    global INSTRUMENTS, RISK, SAME_SIDE_CAP, ACTIVE_PROFILE, DAILY_HALT
    # Chosen by the user: each order risks its strategy's own risk gene (1–5 %), at most 20 % in one direction, and a
    # 10 % daily loss (the leader's result not counted) closes the other members' positions until tomorrow. The leader: see LEADER_RISK.
    INSTRUMENTS, RISK, SAME_SIDE_CAP, DAILY_HALT = list(EVOLVED_INSTRUMENTS), RISK_RANGE[0], .20, EVOLVED_DAILY_HALT
    if isinstance(universe, list) and universe and len(set(universe)) == len(universe) and all(
            isinstance(i, str) and re.fullmatch(r'[A-Z0-9]+-USDT-SWAP', i) for i in universe):
        INSTRUMENTS = list(universe)
    ACTIVE_PROFILE = dict(mode='evolved', active=False, until=None, instruments=list(INSTRUMENTS), scan_limit=len(INSTRUMENTS), risk=RISK, risk_max=RISK_RANGE[1], same_side_cap=SAME_SIDE_CAP, daily_halt=DAILY_HALT,
                          leader_risk=LEADER_RISK, leader_leverage=LEADER_LEVERAGE)
    return ACTIVE_PROFILE


def configure_volume_test(now=None, universe=None):
    global INSTRUMENTS, RISK, SAME_SIDE_CAP, DAILY_HALT
    DAILY_HALT = .10
    p = volume_profile(now)
    INSTRUMENTS, RISK = p['instruments'], p['risk']
    if p['mode'] == 'aggressive' and isinstance(universe, list) and len(universe) == p['scan_limit'] and len(set(universe)) == len(universe) and all(isinstance(i,str) and re.fullmatch(r'[A-Z0-9]+-USDT-SWAP',i) for i in universe):
        INSTRUMENTS = list(universe)
        p['instruments'] = INSTRUMENTS
    SAME_SIDE_CAP = p['same_side_cap']
    return p


class OKXError(RuntimeError):
    pass


class OKX:
    """Minimal signed client for OKX demo trading."""
    def __init__(self, key, secret, passphrase, curl='curl', attempts=3):
        self.key, self.secret, self.passphrase, self.curl = key, secret, passphrase, curl
        self.attempts = 1 if os.environ.get('PANEL_RUNTIME_CONTROL_FILE') else attempts

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
            timeout = '5' if os.environ.get('PANEL_RUNTIME_CONTROL_FILE') else '20'
            out = subprocess.run([self.curl, '-sS', '-m', timeout, '-K', '-'], input=config, capture_output=True, text=True)
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


def enrich_closes(mirror, state, actions):
    """Fill in realised PnL for closes logged without it (e.g. before this existed), from OKX position history.
    Results are cached in state so each close is looked up once."""
    state.pop('close_results', None); state.pop('close_results_v2', None)   # v2 could attach an older close of the same coin
    cache = state.setdefault('close_results_v3', {})
    for a in actions:
        if a.get('action') not in ('closed_on_exchange', 'close') or a.get('execution_test') or a.get('pnl') is not None:
            continue                                # (a pnl already there was read from the exchange when the close was logged)
        key = f"{a['ts']}|{a['inst']}"
        if key not in cache:
            ts = int(datetime.fromisoformat(a['ts'].replace('Z', '+00:00')).timestamp() * 1000)
            # The mirror this close belongs to: the latest one on that coin opened before the close (not the first ever).
            done = [m for m in state.get('mirrored', {}).values() if m.get('inst') == a['inst'] and m.get('status') == 'done'
                    and m.get('opened_at') and int(m['opened_at']) <= ts]
            m = max(done, key=lambda m: int(m['opened_at']), default=None)
            if not m:
                continue
            try:
                res = mirror.fill_result(a['inst'], m.get('posSide', 'net'), int(m['opened_at']), ts + 120000)
            except OKXError:
                continue
            if not res:
                continue                            # not in the exchange history yet: try again next run
            cache[key] = mirror.outcome(m, res) if a['action'] == 'closed_on_exchange' else {k: res[k] for k in ('pnl', 'price_pnl', 'funding', 'close_price', 'fee')}
        a.update(cache[key])


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


def decision_snapshot(data):
    """Publish only fixed signal fields, never source notes, chat or order identifiers."""
    root = Path(data); rows = []
    try:
        with (root/'combined/events.jsonl').open() as f: lines = deque(f,maxlen=2000)
    except OSError: lines = []
    reasons = {'same_side_risk','max_positions','daily_halt','already_at_mean','insufficient_margin','stop','target','timeout'}
    for line in lines:
        try:
            raw = json.loads(line)
            if not isinstance(raw,dict) or not isinstance(raw.get('ts'),str): continue
            if raw.get('type') not in {'signal','skip','daily_halt'}: continue
            stamp = datetime.fromisoformat(raw['ts'].replace('Z','+00:00'))
            if stamp.tzinfo is None: continue
            inst = raw.get('inst')
            if inst is not None and not re.fullmatch(r'[A-Z0-9]+-USDT-SWAP',inst): continue
            rows.append(dict(ts=stamp.isoformat(),type=raw['type'],inst=inst,
                             strategy=raw.get('strategy') if raw.get('strategy') in TAG else None,
                             side=raw.get('side') if raw.get('side') in (-1,1) else None,
                             reason=raw.get('reason') if raw.get('reason') in reasons else None))
        except (ValueError,KeyError,TypeError): continue
    try: updated = json.loads((root/'summary.json').read_text()).get('updated_at')
    except (OSError,ValueError): updated = None
    return dict(strategy_updated_at=updated,strategy_decisions=sorted(rows,key=lambda r:r['ts'],reverse=True)[:50])


class Mirror:
    def __init__(self, client, state, paper, now, dry_run=False):
        self.c, self.s, self.paper, self.now, self.dry = client, state, paper, now, dry_run
        self.actions = []
        self.new_order_risk = {}
        self.now_ms = int(now.timestamp() * 1000)

    def log(self, kind, inst, **kw):
        self.actions.append(dict(ts=self.now.isoformat(timespec='seconds').replace('+00:00', 'Z'), action=kind, inst=inst, dry_run=self.dry, **kw))

    def post(self, path, body):
        if path == '/api/v5/trade/order' and body.get('attachAlgoOrds') and not entries_allowed():
            return dict(code='RUNTIME_PAUSED', msg='New entries blocked by runtime stop latch', data=[])
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
        self.positions, self.all_positions = {}, {}
        managed = set(INSTRUMENTS) | {m['inst'] for m in self.s['mirrored'].values() if m.get('status') == 'open'}
        for p in (pos.get('data') or []) if ok(pos) else []:
            if float(p.get('pos') or 0) == 0:
                continue
            key = (p.get('instId'), p.get('posSide', 'net'))
            # Every USDT-margined swap is shown on the dashboard (manual ones marked as not ours);
            # only managed instruments are ever traded, amended or closed by this program.
            if str(p.get('instId', '')).endswith('-USDT-SWAP'):
                self.all_positions[key] = p
            if p.get('instId') in managed:
                self.positions[key] = p
        algos = self.c.request('GET', '/api/v5/trade/orders-algo-pending?instType=SWAP&ordType=conditional')
        oco = self.c.request('GET', '/api/v5/trade/orders-algo-pending?instType=SWAP&ordType=oco')
        self.algos = [a for r in (algos, oco) if ok(r) for a in r.get('data') or [] if a.get('instId') in managed]
        self.specs = {}

    def spec(self, inst):
        if inst not in self.specs:
            r = self.c.request('GET', f'/api/v5/public/instruments?instType=SWAP&instId={inst}')
            d = (r.get('data') or [{}])[0]
            self.specs[inst] = dict(ctVal=float(d.get('ctVal') or 0), lotSz=d.get('lotSz') or '1', minSz=float(d.get('minSz') or 0), tickSz=d.get('tickSz') or '0.0001',
                                    maxLever=int(float(d.get('lever') or 0)))
        return self.specs[inst]

    def price(self, inst):
        r = self.c.request('GET', f'/api/v5/market/ticker?instId={inst}')
        return float((r.get('data') or [{}])[0].get('last') or 0) if ok(r) else 0.0

    def late_ok(self, inst, p):
        """An entry older than OPEN_WINDOW is still worth following when the price has not moved far from the paper
        entry: the distance left to the stop is 50–150 % of the paper stop distance (open() sizes by the distance left,
        so the risk in USDT is the same as for a fresh entry)."""
        px, entry, stop = self.price(inst), float(p.get('entry') or 0), float(p.get('stop') or 0)
        if not px or not entry or not stop or entry == stop:
            return False
        left = (px - stop) * p['side'] / abs(entry - stop)
        return LATE_BAND[0] <= left <= LATE_BAND[1]

    def held(self, inst, side):
        ps = 'long' if side == 1 else 'short'
        return self.positions.get((inst, ps)) or self.positions.get((inst, 'net'))

    # ---- actions ---------------------------------------------------------------
    def open(self, key, inst, p):
        if not entries_allowed():
            return self.log('skip', inst, reason='runtime_paused')
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
        r = RISK
        evolved = bool(ACTIVE_PROFILE and ACTIVE_PROFILE.get('mode') == 'evolved')
        leader = evolved and bool(p.get('leader'))
        if leader:
            r = LEADER_RISK
        elif evolved and p.get('risk') is not None:
            r = min(RISK_RANGE[1], max(RISK_RANGE[0], float(p['risk'])))
        risk = r * self.equity
        same_risk = 0.0
        for existing_key, m in self.s['mirrored'].items():
            if leader or m.get('leader'):          # the leader is outside the same-side cap, both ways
                continue
            if m.get('status') != 'open' or m.get('side') != side:
                continue
            if existing_key in self.new_order_risk:
                same_risk += self.new_order_risk[existing_key]
                continue
            held = self.held(m['inst'], side)
            if not held:
                continue
            avg = float(held.get('avgPx') or m.get('entry') or 0)
            if avg <= 0 or not m.get('stop'):
                return self.log('skip', inst, reason='existing_risk_unknown')
            same_risk += max((avg - float(m['stop'])) * side, 0) * abs(float(held['pos'])) * self.spec(m['inst'])['ctVal']
        if not leader and same_risk + risk > SAME_SIDE_CAP * self.equity * (1 + 1e-9):
            return self.log('skip', inst, reason='same_side_risk')
        contracts = quantize(risk / (px * dist) / sp['ctVal'], sp['lotSz'])
        if float(contracts) < sp['minSz'] or contracts <= 0:
            return self.log('skip', inst, reason='size_below_minimum', contracts=fmt(contracts), min=sp['minSz'])
        cap = LEADER_LEVERAGE if leader else MAX_LEVERAGE
        lever = max(1, min(cap, int(p.get('leverage') or cap), int(.8 / dist), sp.get('maxLever') or cap))
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
                                       stop=p['stop'], target=p.get('tp'), leverage=lever, entry=px, clOrdId=cl, opened_at=self.now_ms, status='open', leader=leader,
                                       ordId=(r.get('data') or [{}])[0].get('ordId'))
        self.new_order_risk[key] = float(contracts) * sp['ctVal'] * abs(px - p['stop'])
        self.available -= margin
        self.log('open', inst, side=side, strategy=p.get('strategy'), contracts=fmt(contracts), price=px, stop=algo['slTriggerPx'],
                 target=algo.get('tpTriggerPx'), leverage=lever, risk_usdt=round(risk, 2), **({'leader': True} if leader else {}))

    def fill_result(self, inst, pos_side='net', since_ms=0, until_ms=None):
        """Realised result of the latest close of `inst` from OKX position history (read only), or None."""
        r = self.c.request('GET', f'/api/v5/account/positions-history?instType=SWAP&instId={inst}&limit=20')
        rows = [x for x in (r.get('data') or []) if ok(r) and x.get('instId') == inst
                and (pos_side in ('net', None) or x.get('posSide', 'net') in (pos_side, 'net'))
                and int(x.get('uTime') or 0) >= since_ms and (until_ms is None or int(x.get('uTime') or 0) <= until_ms)]
        if not rows:
            return None
        x = max(rows, key=lambda x: int(x.get('uTime') or 0))
        num = lambda k: float(x[k]) if x.get(k) not in (None, '') else None
        return dict(pnl=num('realizedPnl'), price_pnl=num('pnl'), funding=num('fundingFee'), close_price=num('closeAvgPx'), open_price=num('openAvgPx'), fee=num('fee'),
                    closed_at=int(x.get('uTime') or 0), liquidated=str(x.get('type')) in ('3', '4'))

    def outcome(self, m, res):
        """Which exchange order closed it: the stop, the target, or a liquidation."""
        if not res:
            return {}
        px, stop, target = res.get('close_price'), m.get('stop'), m.get('target')
        kind = 'liquidation' if res.get('liquidated') else None
        if not kind and px and stop and target:
            kind = 'stop' if abs(px - float(stop)) <= abs(px - float(target)) else 'target'
        elif not kind and res.get('pnl') is not None:
            kind = 'target' if res['pnl'] >= 0 else 'stop'
        return dict({k: v for k, v in res.items() if k in ('pnl', 'price_pnl', 'funding', 'close_price', 'fee')}, outcome=kind)

    def close(self, key, m, reason):
        body = dict(instId=m['inst'], mgnMode='isolated', autoCxl=True, **({'posSide': m['posSide']} if m['posSide'] != 'net' else {}))
        r = self.post('/api/v5/trade/close-position', body)
        if not ok(r):
            return self.log('error', m['inst'], step='close', detail=first_error(r))
        m['status'] = 'done'; m['closed_reason'] = reason
        for k in [k for k in self.positions if k[0] == m['inst'] and k[1] in (m['posSide'], 'net')]:
            self.positions.pop(k)  # gone now, so a fresh paper entry on this coin can be mirrored in the same run
        try:  # the close fills at market; its history row is usually there at once, otherwise the summary fills it in later
            res = self.fill_result(m['inst'], m['posSide'], int(m.get('opened_at') or 0))
        except OKXError:
            res = None
        extra = {k: res[k] for k in ('pnl', 'price_pnl', 'funding', 'close_price', 'fee')} if res else {}
        self.book_leader(m, res)
        self.log('close', m['inst'], strategy=m.get('strategy'), reason=reason, **extra)

    def amend_stop(self, key, m, new_stop):
        algo = next((a for a in self.algos if a['instId'] == m['inst'] and (a.get('posSide', 'net') == m['posSide'] or m['posSide'] == 'net')), None)
        if not algo:
            return self.log('error', m['inst'], step='amend_stop', detail='algo order not found')
        tick = self.spec(m['inst'])['tickSz']
        r = self.post('/api/v5/trade/amend-algos', dict(instId=m['inst'], algoId=algo['algoId'], newSlTriggerPx=fmt(quantize(new_stop, tick, ROUND_HALF_UP))))
        if not ok(r):
            return self.log('error', m['inst'], step='amend_stop', detail=first_error(r))
        self.log('amend_stop', m['inst'], old=m['stop'], new=new_stop); m['stop'] = new_stop

    def leader_upl(self):
        """Floating PnL of the leader's open positions, and remember each one's so a later close can be booked."""
        total = 0.0
        for m in self.s['mirrored'].values():
            if m.get('status') == 'open' and m.get('leader'):
                held = self.held(m['inst'], m['side'])
                if held:
                    m['last_upl'] = float(held.get('upl') or 0); total += m['last_upl']
        return total

    def book_leader(self, m, res):
        """A leader position closed: its result leaves the daily-halt arithmetic (exchange result, else the last floating PnL)."""
        if m.get('leader'):
            pnl = (res or {}).get('pnl')
            self.s['leader_realized'] = float(self.s.get('leader_realized') or 0) + float(pnl if pnl is not None else m.get('last_upl') or 0)

    def flatten(self, reason, keep_leader=False):
        for key, m in self.s['mirrored'].items():
            if keep_leader and m.get('leader'):
                continue
            if m['status'] == 'open' and self.held(m['inst'], m['side']):
                self.close(key, m, reason)
            elif m['status'] == 'open':
                m['status'] = 'done'

    # ---- one reconciliation pass ---------------------------------------------------
    def run(self, action='run'):
        self.load()
        day = local_day(self.now)
        leader_upl = self.leader_upl()
        if self.s.get('day') != day:
            self.s.update(day=day, day_start=self.equity, halted=False, leader_realized=0.0, leader_upl_start=leader_upl)
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
        # Daily halt, counted without the leader: today's change in equity minus what the leader's positions made or lost
        # today. It closes and blocks the other members only; the leader keeps trading.
        leader_day = float(self.s.get('leader_realized') or 0) + leader_upl - float(self.s.get('leader_upl_start') or 0)
        others_day = self.equity - (self.s.get('day_start') or self.equity) - leader_day
        if not self.s['halted'] and self.s.get('day_start') and others_day <= -self.s['day_start'] * DAILY_HALT:
            self.s['halted'] = True
            self.log('daily_halt', '-', equity=self.equity, day_start=self.s['day_start'], others_day=round(others_day, 2), leader_day=round(leader_day, 2))
            self.flatten('daily_halt', keep_leader=True)
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
                try:
                    res = self.outcome(m, self.fill_result(m['inst'], m['posSide'], int(m.get('opened_at') or 0)))
                except OKXError:
                    res = {}
                if res.get('pnl') is not None:
                    m['realized_pnl'] = res['pnl']
                self.book_leader(m, res)
                self.log('closed_on_exchange', m['inst'], strategy=m.get('strategy'), **res)
            elif not same:
                self.close(key, m, 'paper_closed')
            elif abs(p['stop'] - m['stop']) / m['stop'] > AMEND_TOLERANCE and (p['stop'] - m['stop']) * m['side'] > 0:
                self.amend_stop(key, m, p['stop'])
        # New entries (a dry run previews them as if resumed).
        if self.s['paused'] and not self.dry:
            return
        # Priority when the same-side cap fills up: the higher-ranked team member first (rank 1 = leader), then the coin list order.
        order = lambda kv: (kv[1].get('rank', 99), INSTRUMENTS.index(kv[0]) if kv[0] in INSTRUMENTS else 99, kv[0])
        for inst, p in sorted(paper_positions.items(), key=order):
            key = f"{inst}|{int(p['opened'])}"
            if key in self.s['mirrored'] or inst not in INSTRUMENTS:
                continue
            if self.s['halted'] and not p.get('leader'):       # halted for the day: only the leader still opens
                continue
            if self.held(inst, p['side']) or any(k[0] == inst for k in self.positions):
                self.log('skip', inst, reason='exchange_position_exists_not_ours'); self.s['mirrored'][key] = dict(inst=inst, status='skipped', side=p['side'], posSide='net')
                continue
            if self.now_ms - int(p['opened']) > OPEN_WINDOW and action != 'follow-current' and not self.late_ok(inst, p):
                self.s['mirrored'][key] = dict(inst=inst, status='skipped', side=p['side'], posSide='net', reason='too_old')
                self.log('skip', inst, reason='paper_entry_too_old')
                continue
            self.open(key, inst, p)

    def settle(self):
        """load() ran before this run's orders: read the account again so the summary already lists new / closed positions
        (otherwise they only appear after the next scheduled run, 40–60 minutes later)."""
        if not self.dry and any(a['action'] in ('open', 'close', 'amend_stop', 'flatten', 'daily_halt') for a in self.actions):
            self.load()

    def summary(self):
        pos = [dict(inst=p['instId'], side=p.get('posSide', 'net'), contracts=p.get('pos'), avgPx=p.get('avgPx'), markPx=p.get('markPx'),
                    upl=p.get('upl'), uplRatio=p.get('uplRatio'), lever=p.get('lever'), margin=p.get('margin'), ctVal=self.spec(p['instId'])['ctVal'],
                    ours=any(m['status'] == 'open' and m['inst'] == p['instId'] for m in self.s['mirrored'].values()))
               for p in getattr(self, 'all_positions', self.positions).values()]
        return dict(updated_at=self.now.isoformat(timespec='seconds').replace('+00:00', 'Z'), equity=self.equity, available=self.available,
                    day_start=self.s.get('day_start'), paused=self.s['paused'], halted=self.s['halted'], dry_run=self.dry, positions=pos,
                    stop_control=self.s.get('stop_control'),
                    execution_profile=dict(ACTIVE_PROFILE or volume_profile(self.now), instruments=list(INSTRUMENTS), risk=RISK, same_side_cap=SAME_SIDE_CAP),
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
    creds = [os.environ.get(k, '').strip() for k in ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE')]
    if not all(creds):
        print('未配置 OKX 模拟交易密钥，跳过跟单。'); return
    # Signal source: the evolution champion's paper positions when present (tools/evolve), else the retired strategy engine.
    evolved_path, paper_path = args.data / 'evolve' / 'signals.json', args.data / 'combined' / 'state.json'
    if evolved_path.exists():
        sig = json.loads(evolved_path.read_text())
        paper = {'positions': sig.get('positions', {}), 'source': 'evolve'}
        applied = configure_evolved(sig.get('instruments'))
        print(f"跟随进化冠军 · 每笔风险按策略 {RISK_RANGE[0]:.0%}–{RISK_RANGE[1]:.0%} · 同向 ≤ {SAME_SIDE_CAP:.0%} · 日亏 {DAILY_HALT:.0%} 停手 · {len(INSTRUMENTS)} 币")
    else:
        if not paper_path.exists():
            print('没有策略信号，跳过跟单。'); return
        paper = json.loads(paper_path.read_text())
        applied = configure_volume_test(universe=paper.get('universe') if paper.get('profile_mode') == 'aggressive' else None)
        print(describe_volume(applied))
    if args.action in ('run', 'resume', 'follow-current', 'dry-run') and applied['mode'] == 'aggressive' and len(INSTRUMENTS) != applied['scan_limit']:
        raise SystemExit('Aggressive execution requires the confirmed 100-contract signal universe; no orders submitted')
    out = args.data / 'okx_demo'; out.mkdir(parents=True, exist_ok=True)
    state_path = out / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else new_state()
    dry = args.action == 'dry-run'
    mirror = Mirror(OKX(*creds), json.loads(json.dumps(state)) if dry else state, paper, datetime.now(timezone.utc), dry)
    error = None
    try:
        if args.action == 'snapshot':
            mirror.load()  # Read-only exchange refresh: no order, leverage or stop mutations.
        else:
            mirror.run('run' if dry else args.action)
            mirror.settle()
    except OKXError as exc:
        error = str(exc); mirror.log('error', '-', step='run', detail=error)
    for a in mirror.actions:
        print(json.dumps(a, ensure_ascii=False))
    if not dry and hasattr(mirror, 'equity'):
        mirror.record_equity()
    if not dry:
        atomic_json(state_path, state)
        with (out / 'actions.jsonl').open('a') as f:
            f.writelines(json.dumps(a, ensure_ascii=False) + '\n' for a in mirror.actions)
        if hasattr(mirror, 'equity'):
            summary = mirror.summary()
            summary.update(decision_snapshot(args.data))
            lines = (out / 'actions.jsonl').read_text().splitlines()[-30:]
            summary['recent_actions'], tests_closed = dashboard_actions(out, [json.loads(l) for l in lines])
            enrich_closes(mirror, state, summary['recent_actions'])
            for a in summary['recent_actions']:      # the biggest single close so far, kept so the dashboard can put the headline return in context
                if a.get('action') in ('close', 'closed_on_exchange') and isinstance(a.get('pnl'), (int, float)) \
                        and abs(a['pnl']) > abs((state.get('largest_close') or {}).get('pnl', 0)):
                    state['largest_close'] = dict(ts=a['ts'], inst=a['inst'], pnl=a['pnl'])
            summary['largest_close'] = state.get('largest_close')
            atomic_json(state_path, state)  # keep looked-up close results so each is fetched once
            summary['execution_test_trades'] = tests_closed
            summary['closed_trades'] += tests_closed
            atomic_json(out / 'summary.json', summary)
    if error:
        sys.exit(1)


if __name__ == '__main__':
    main()
