#!/usr/bin/env python3
"""Paper trader for learning: one 100 USDT paper account traded by three rule sets.

It is a simulation only. It reads OKX public market data (no keys, no orders). The three strategies
share one account (cash, same-direction risk cap and daily halt); a coin holds at most one position,
opened by whichever strategy signals first, and every trade records the strategy that made it:

  mean_reversion  -- entry rules of assets/indicators.mjs (mirror of bt60.engine.make_signal),
                     RSI 25 / trend 2%; stop 1.5xATR14 (0.5%..4%), target 20-bar mid band, 24h time stop.
  breakout_long   -- long-only Donchian: close > prior 48h high with EMA50 > EMA200; stop 2xATR14
                     (0.5%..4%), 3xATR chandelier trailing stop, 168h time stop, at most 6 positions.
  breakout_daily  -- the same long-only Donchian on daily (UTC) candles: 20-day high, EMA50 > EMA200,
                     stop 2x daily ATR14 (0.5%..25%), 3xATR trailing stop, 90-day time stop, at most 6 positions.

Shared rules (identical to the research backtests): signals on closed bars, fill at the next bar's
open, 1% of account equity risked per trade, isolated 10x/5x by trailing 24h turnover (>= 50M USDT), same-direction
open risk <= 5% of the account, one position per coin across strategies, daily (UTC+7) 8% loss halt, taker 0.05% + 0.02% slippage per
side, stop assumed first when stop and target fall inside one bar, loss capped at posted margin.

Hourly strategies act on each closed 1H bar; the daily strategy acts when a UTC day closes. Each run
only processes bars newer than the saved state, so reruns are idempotent and missed hours
are caught up from the fetched history. A fresh account can be started N days in the past with
--backfill-days: the same code replays those days hour by hour, then continues live; `live_since`
marks where the replay ends. --reset deletes all account data first. Standard library only.
"""
import argparse
import csv
import json
import math
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from volume_test import profile as volume_profile, describe as describe_volume
from runtime_guard import check_scan, atomic_json

COINS = 'BTC ETH SOL XRP DOGE'.split()  # the five most liquid majors, chosen for the learning run
INSTRUMENTS = [f'{c}-USDT-SWAP' for c in COINS]
HOUR = 3_600_000
BAR_MS = {'1H': HOUR, '1Dutc': 24 * HOUR}
FEE, SLIP = 0.0005, 0.0002
START_EQUITY = 100.0
STRATEGIES = {
    'mean_reversion': dict(name='均值回归', kind='mr', bar='1H', rsi=25, trend=.02, stop_k=1.5, stop_cap=.04, trail_k=None, hold=24, max_pos=None, long_only=False, turn_window=24),
    'breakout_long': dict(name='趋势突破只做多', kind='donchian', bar='1H', lookback=48, stop_k=2.0, stop_cap=.04, trail_k=3.0, hold=168, max_pos=6, long_only=True, turn_window=24),
    # Daily stops are wider than 4%, so leverage is lowered when needed to keep liquidation beyond the stop.
    'breakout_daily': dict(name='日线趋势突破', kind='donchian', bar='1Dutc', lookback=20, stop_k=2.0, stop_cap=.25, trail_k=3.0, hold=90, max_pos=6, long_only=True, turn_window=1),
}
RISK, SAME_SIDE_CAP, DAILY_HALT = .01, .05, .08
UTC7 = timezone(timedelta(hours=7))


def configure_volume_test(now=None, universe=None):
    global INSTRUMENTS, RISK, SAME_SIDE_CAP
    p = volume_profile(now)
    INSTRUMENTS, RISK = p['instruments'], p['risk']
    if p['mode'] == 'aggressive' and universe is not None:
        INSTRUMENTS = list(universe)
        p['instruments'] = INSTRUMENTS
    SAME_SIDE_CAP = p['same_side_cap']
    STRATEGIES['mean_reversion']['rsi'] = p['rsi']
    STRATEGIES['mean_reversion']['trend'] = p['trend']
    STRATEGIES['breakout_long']['lookback'] = p['lookback']
    return p


# ---------------------------------------------------------------- market data
_request_lock = threading.Lock()
_next_request = 0.0


def okx_get(path, retries=5):
    global _next_request
    if os.environ.get('PANEL_RUNTIME_CONTROL_FILE'):
        retries = min(retries, 2)
    for attempt in range(retries):
        check_scan()
        # Shared across scanner workers: comfortably below history-candles' 20 requests / 2 s.
        with _request_lock:
            delay = _next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            _next_request = time.monotonic() + .15
        try:
            check_scan()
            timeout = '5' if os.environ.get('PANEL_RUNTIME_CONTROL_FILE') else '20'
            out = subprocess.run(['curl', '-sS', '-m', timeout, 'https://www.okx.com/api/v5/' + path],
                                 capture_output=True, text=True, check=True).stdout
            body = json.loads(out)
            if body.get('code') == '0':
                return body['data']
        except (subprocess.CalledProcessError, ValueError):
            pass
        time.sleep(1 + attempt)
    raise RuntimeError(f'OKX request failed: {path}')


def ranked_universe(tickers, specs, limit=100):
    allowed = {s.get('instId') for s in specs if s.get('state') == 'live' and s.get('settleCcy') == 'USDT' and s.get('ctType') == 'linear'}
    ranked = {}
    for t in tickers:
        inst = t.get('instId', '')
        if inst not in allowed or not re.fullmatch(r'[A-Z0-9]+-USDT-SWAP', inst):
            continue
        try:
            price, volume = float(t['last']), float(t['volCcy24h'])
            turnover = price * volume
        except (KeyError, TypeError, ValueError):
            continue
        if price > 0 and volume > 0 and math.isfinite(turnover):
            ranked[inst] = turnover
    selected = sorted(ranked, key=lambda inst: (-ranked[inst], inst))[:limit]
    if len(selected) < limit:
        raise RuntimeError(f'Only {len(selected)} eligible liquid contracts; expected {limit}, preserving prior account data')
    return selected


def fetch_open(inst, bar='1H'):
    """The bar currently forming (not yet closed): only its open price is used, to fill pending orders."""
    data = okx_get(f'market/candles?instId={inst}&bar={bar}&limit=1')
    r = data[0] if data else None
    return dict(ts=int(r[0]), o=float(r[1])) if r and len(r) >= 9 and r[8] == '0' else None


def fetch_price(inst):
    """Latest traded price, recorded with each run so the dashboard can show floating PnL without reaching OKX itself."""
    data = okx_get(f'market/ticker?instId={inst}')
    try:
        px = float(data[0]['last']) if data else None
    except (KeyError, TypeError, ValueError):
        px = None
    return px if px and px > 0 else None


def fetch_okx(inst, bar='1H', pages=5):
    """Closed bars (1H or 1Dutc), oldest first: dicts with ts/o/h/l/c/q (q = quote turnover)."""
    rows, after = {}, ''
    for _ in range(pages):
        data = okx_get(f'market/history-candles?instId={inst}&bar={bar}&limit=100' + (f'&after={after}' if after else ''))
        if not data:
            break
        for r in data:
            if len(r) >= 9 and r[8] == '1':
                rows[int(r[0])] = dict(ts=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), q=float(r[7]))
        after = data[-1][0]
        time.sleep(.1)
    return [rows[k] for k in sorted(rows)]


# ---------------------------------------------------------------- indicators
def ewm(values, alpha):
    out, prev = [], None
    for v in values:
        if v is None:
            out.append(prev)
            continue
        prev = v if prev is None else alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def indicators(bars):
    c = [b['c'] for b in bars]; h = [b['h'] for b in bars]; l = [b['l'] for b in bars]; q = [b['q'] for b in bars]
    n = len(bars)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    gains = [None] + [max(c[i] - c[i - 1], 0) for i in range(1, n)]
    losses = [None] + [max(c[i - 1] - c[i], 0) for i in range(1, n)]
    g, s = ewm(gains, 1 / 14), ewm(losses, 1 / 14)
    rsi = [100 - 100 / (1 + g[i] / s[i]) if i >= 14 and s[i] else None for i in range(n)]
    mid, lo, hi = [None] * n, [None] * n, [None] * n
    for i in range(19, n):
        w = c[i - 19:i + 1]; m = sum(w) / 20; sd = (sum((x - m) ** 2 for x in w) / 20) ** .5
        mid[i], lo[i], hi[i] = m, m - 2 * sd, m + 2 * sd
    gap_ok = [False] + [bars[i]['ts'] - bars[i - 1]['ts'] == HOUR for i in range(1, n)]
    run = 0; contiguous = []
    for ok in gap_ok:
        run = run + 1 if ok else 0
        contiguous.append(run >= 200)
    return dict(c=c, h=h, l=l, q=q, e50=ewm(c, 2 / 51), e200=ewm(c, 2 / 201), atr=ewm(tr, 1 / 14), rsi=rsi,
                mid=mid, lo=lo, hi=hi, contiguous=contiguous)


def signal(x, i, cfg):
    """Side (+1/-1/0) for the bar at index i, evaluated after it closed."""
    if cfg['kind'] == 'mr':
        if i < 200 or not x['contiguous'][i] or x['lo'][i - 1] is None or x['rsi'][i - 1] is None:
            return 0
        c, atr = x['c'], x['atr'][i]
        avg = sum(x['q'][i - 20:i]) / 20
        common = (abs(x['e50'][i] / x['e200'][i] - 1) < cfg['trend'] and atr > 0 and abs(x['e50'][i] - x['e50'][i - 5]) / atr < .8
                  and .002 <= atr / c[i] <= .035 and avg > 0 and x['q'][i] / avg >= .8)
        if not common:
            return 0
        if x['rsi'][i - 1] < cfg['rsi'] and c[i - 1] < x['lo'][i - 1] and c[i] >= x['lo'][i]:
            return 1
        if x['rsi'][i - 1] > 100 - cfg['rsi'] and c[i - 1] > x['hi'][i - 1] and c[i] <= x['hi'][i]:
            return -1
        return 0
    lb = cfg['lookback']
    if i < max(199, lb):
        return 0
    if x['e50'][i] > x['e200'][i] and x['c'][i] > max(x['h'][i - lb:i]):
        return 1
    if not cfg['long_only'] and x['e50'][i] < x['e200'][i] and x['c'][i] < min(x['l'][i - lb:i]):
        return -1
    return 0


# ---------------------------------------------------------------- account replay
ACCOUNT, ACCOUNT_NAME = 'combined', '合并账户'
ORDER = list(STRATEGIES)  # tie-break when strategies signal the same coin on the same bar


def new_account():
    return dict(cash=START_EQUITY, positions={}, pending={}, last_marks={}, last_ts=None, day=None, day_start=START_EQUITY, halted=False)


def local_day(ts):
    return datetime.fromtimestamp(ts / 1000, UTC7).date().isoformat()


def iso(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def mark(acct, prices):
    eq = acct['cash']
    for inst, p in acct['positions'].items():
        px = prices.get(inst, p['entry'])
        eq += max(p['side'] * p['qty'] * (px - p['entry']), -p['margin'])
    return eq


def replay(acct, books, start_ts=None):
    """Advance the shared account over closed bars newer than acct['last_ts'] (hourly timeline).

    books maps a bar size ('1H', '1Dutc') to (series, indicators, forming-bar opens). Returns (trades, points, events).
    """
    hs, hind, hopens = books['1H']
    ds, dind, _ = books.get('1Dutc', ({}, {}, {}))
    hidx = {inst: {b['ts']: i for i, b in enumerate(bars)} for inst, bars in hs.items()}
    didx = {inst: {b['ts']: i for i, b in enumerate(bars)} for inst, bars in ds.items()}
    times = sorted({b['ts'] for bars in hs.values() for b in bars})
    trades, points, events = [], [], []
    if acct['last_ts'] is None:  # fresh account: start at the latest closed bar, or at start_ts to backfill
        anchor = [t for t in times if start_ts is None or t <= start_ts]
        if not anchor:
            return trades, points, events
        acct['last_ts'] = anchor[-1]
        acct['day'], acct['day_start'] = local_day(anchor[-1]), acct['cash']
        points.append((anchor[-1], round(acct['cash'], 6)))
        events.append(dict(ts=iso(anchor[-1]), type='started', equity=acct['cash'], backfill=start_ts is not None))
        if start_ts is None:
            return trades, points, events
    new = [t for t in times if t > acct['last_ts']]
    if new and new[0] - acct['last_ts'] > HOUR:
        events.append(dict(ts=iso(new[0]), type='gap', missing_bars=(new[0] - acct['last_ts']) // HOUR - 1))
    last_close = dict(acct.get('last_marks', {}))

    def fill(inst, bar, t):
        """Fill a pending order at this bar's open (sizing and checks against the shared account)."""
        o = acct['pending'].pop(inst); s = o['side']; cfg = STRATEGIES[o['strategy']]
        entry = bar['o'] * (1 + SLIP * s)
        dist = min(max(cfg['stop_k'] * o['atr'] / entry, .005), cfg.get('stop_cap', .04))
        tp = o.get('mid')
        skip = lambda reason: events.append(dict(ts=iso(t), type='skip', inst=inst, strategy=o['strategy'], reason=reason))
        if tp is not None and (tp - entry) * s <= 0:
            return skip('already_at_mean')
        equity = mark(acct, last_close)
        same = sum(p['risk'] for p in acct['positions'].values() if p['side'] == s)
        if same + o['risk'] > SAME_SIDE_CAP * equity:
            return skip('same_side_risk')
        if cfg['max_pos'] is not None and sum(p['strategy'] == o['strategy'] for p in acct['positions'].values()) >= cfg['max_pos']:
            return skip('max_positions')
        qty = o['risk'] / (entry * dist)
        lev = 10 if o['turn24'] >= 50e6 else 5
        if dist >= .8 / lev:  # keep liquidation beyond the stop (only wide daily stops hit this)
            lev = max(1, int(.8 / dist))
        margin = qty * entry / lev
        if margin > equity - sum(p['margin'] for p in acct['positions'].values()):
            return skip('margin')
        acct['positions'][inst] = dict(strategy=o['strategy'], side=s, entry=entry, stop=entry * (1 - s * dist), tp=tp, qty=qty, margin=margin,
                                       risk=o['risk'], leverage=lev, opened=t, bars=0)
        events.append(dict(ts=iso(t), type='trade_opened', inst=inst, strategy=o['strategy'], side=s, price=round(entry, 8),
                           stop=round(entry * (1 - s * dist), 8), target=None if tp is None else round(tp, 8), leverage=lev))

    def manage(inst, p, bar, atr, t):
        """Exit or trail one position on a closed bar of its own strategy's size."""
        cfg, s = STRATEGIES[p['strategy']], p['side']
        ex = reason = None
        if (s == 1 and bar['l'] <= p['stop']) or (s == -1 and bar['h'] >= p['stop']):
            ex, reason = (min(bar['o'], p['stop']) if s == 1 else max(bar['o'], p['stop'])), 'stop'
        elif p['tp'] is not None and ((s == 1 and bar['h'] >= p['tp']) or (s == -1 and bar['l'] <= p['tp'])):
            ex, reason = (max(bar['o'], p['tp']) if s == 1 else min(bar['o'], p['tp'])), 'target'
        elif p['bars'] >= cfg['hold']:
            ex, reason = bar['c'], 'timeout'
        if ex is not None:
            gross = max(s * p['qty'] * (ex - p['entry']), -p['margin'])
            fee = FEE * p['qty'] * (p['entry'] + ex)
            acct['cash'] += gross - fee
            row = dict(strategy=p['strategy'], inst=inst, side=s, entry_time=iso(p['opened']), exit_time=iso(t), entry=round(p['entry'], 8),
                       exit=round(ex, 8), qty=p['qty'], leverage=p['leverage'], pnl=round(gross - fee, 6), r=round((gross - fee) / p['risk'], 4), reason=reason)
            trades.append(row)
            events.append(dict(ts=iso(t), type='trade_closed', inst=inst, strategy=p['strategy'], side=s, price=round(ex, 8), pnl=row['pnl'], reason=reason))
            del acct['positions'][inst]
            return
        p['bars'] += 1
        if cfg['trail_k']:
            p['stop'] = max(p['stop'], bar['c'] - cfg['trail_k'] * atr) if s == 1 else min(p['stop'], bar['c'] + cfg['trail_k'] * atr)

    def signals(keys, bars_now, ind, idx, t, equity, fill_ts):
        for key in keys:
            cfg = STRATEGIES[key]
            for inst in sorted(bars_now):
                if inst not in INSTRUMENTS or inst in acct['positions'] or inst in acct['pending']:
                    continue
                i = idx[inst][t]; s = signal(ind[inst], i, cfg)
                if s == 0 or (cfg['long_only'] and s == -1):
                    continue
                turn24 = sum(ind[inst]['q'][max(0, i - cfg.get('turn_window', 24) + 1):i + 1])
                acct['pending'][inst] = dict(strategy=key, side=s, atr=ind[inst]['atr'][i], mid=ind[inst]['mid'][i] if cfg['kind'] == 'mr' else None,
                                             turn24=turn24, risk=RISK * equity, signal_ts=t, fill_ts=fill_ts)
                events.append(dict(ts=iso(t), type='signal', inst=inst, strategy=key, side=s))

    hourly = [k for k in ORDER if STRATEGIES[k].get('bar', '1H') == '1H']
    daily = [k for k in ORDER if STRATEGIES[k].get('bar') == '1Dutc']
    for t in new:
        bars_now = {inst: hs[inst][hidx[inst][t]] for inst in hs if t in hidx[inst]}
        # 1) fill orders whose fill bar starts now (hourly signals: next hour; daily signals: next UTC day)
        for inst in sorted(acct['pending']):
            if inst in bars_now and acct['pending'][inst]['fill_ts'] <= t:
                fill(inst, bars_now[inst], t)
        # 2) manage hourly positions on this closed hour
        for inst, p in sorted(acct['positions'].items()):
            if STRATEGIES[p['strategy']].get('bar', '1H') == '1H' and inst in bars_now:
                manage(inst, p, bars_now[inst], hind[inst]['atr'][hidx[inst][t]], t)
        # 3) when this hour completes a UTC day, the daily bar has closed: manage daily positions on it
        day_bar = t + HOUR - 24 * HOUR  # the daily bar whose last hour is t (UTC days for 1Dutc)
        daily_now = {inst: ds[inst][didx[inst][day_bar]] for inst in ds if day_bar in didx[inst]}
        for inst, p in sorted(acct['positions'].items()):
            if STRATEGIES[p['strategy']].get('bar') == '1Dutc' and inst in daily_now:
                manage(inst, p, daily_now[inst], dind[inst]['atr'][didx[inst][day_bar]], t)
        # 4) mark equity, daily halt
        last_close.update({inst: b['c'] for inst, b in bars_now.items()})
        equity = mark(acct, last_close)
        if local_day(t) != acct['day']:
            acct['day'], acct['day_start'], acct['halted'] = local_day(t), equity, False
        if not acct['halted'] and equity <= acct['day_start'] * (1 - DAILY_HALT):
            acct['halted'] = True; events.append(dict(ts=iso(t), type='daily_halt', equity=round(equity, 6)))
        points.append((t, round(equity, 6)))
        acct['last_ts'] = t
        # 5) new signals; they fill at the next bar of their own size
        if acct['halted']:
            continue
        signals(hourly, bars_now, hind, hidx, t, equity, t + HOUR)
        if daily_now:
            signals(daily, daily_now, dind, didx, day_bar, equity, t + HOUR)
    # The next hour has already opened: its open is known, so pending orders due then fill now.
    # Exits on that bar are still judged when it closes, exactly as before.
    nxt = acct['last_ts'] + HOUR
    for inst in sorted(acct['pending']):
        bar = (hopens or {}).get(inst)
        if bar and bar['ts'] == nxt and bar['o'] > 0 and acct['pending'][inst]['fill_ts'] <= nxt:
            fill(inst, bar, nxt)
    acct['last_marks'] = {k: v for k, v in last_close.items() if k in acct['positions']}  # seeded from the previous run
    return trades, points, events


# ---------------------------------------------------------------- persistence
TRADE_FIELDS = ['strategy', 'inst', 'side', 'entry_time', 'exit_time', 'entry', 'exit', 'qty', 'leverage', 'pnl', 'r', 'reason']


def load_state(path):
    return json.loads(path.read_text()) if path.exists() else new_account()


def append_csv(path, fields, rows):
    new = not path.exists()
    with path.open('a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


def read_trades(path):
    if not path.exists():
        return []
    with path.open(newline='') as f:
        return [dict(r, pnl=float(r['pnl']), r=float(r['r'])) for r in csv.DictReader(f)]


def position_mark(p, price):
    """Floating PnL at the price fetched during this run (loss capped at the posted margin)."""
    if not price:
        return {}
    pnl = max(p['side'] * p['qty'] * (price - p['entry']), -p['margin'])
    return dict(mark=price, unrealized=round(pnl, 6))


def stats(trades):
    wins = [t for t in trades if t['pnl'] > 0]; losses = [t for t in trades if t['pnl'] <= 0]
    return dict(closed_trades=len(trades), win_rate_pct=round(len(wins) / len(trades) * 100, 1) if trades else None,
                avg_r=round(sum(t['r'] for t in trades) / len(trades), 3) if trades else None,
                payoff=round((sum(t['pnl'] for t in wins) / len(wins)) / (-sum(t['pnl'] for t in losses) / len(losses)), 2) if wins and losses and sum(t['pnl'] for t in losses) < 0 else None)


def summarize(acct, data_dir, prices=None):
    trades = read_trades(data_dir / ACCOUNT / 'trades.csv')
    equity = mark(acct, acct.get('last_marks', {}))
    peak, dd = START_EQUITY, 0.0
    eq_path = data_dir / ACCOUNT / 'equity.csv'
    if eq_path.exists():
        with eq_path.open(newline='') as f:
            for r in csv.DictReader(f):
                v = float(r['equity']); peak = max(peak, v); dd = max(dd, (peak - v) / peak * 100)
    by_strategy = [dict(key=k, name=STRATEGIES[k]['name'], pnl=round(sum(t['pnl'] for t in trades if t['strategy'] == k), 4),
                        positions=sum(p['strategy'] == k for p in acct['positions'].values()), **stats([t for t in trades if t['strategy'] == k])) for k in STRATEGIES]
    return dict(name=ACCOUNT_NAME, equity=round(equity, 4), cash=round(acct['cash'], 6), return_pct=round((equity / START_EQUITY - 1) * 100, 2),
                max_drawdown_pct=round(dd, 2), **stats(trades),
                halted=acct['halted'], last_bar=iso(acct['last_ts']) if acct['last_ts'] else None,
                started=acct.get('started'), live_since=acct.get('live_since'), by_strategy=by_strategy,
                open_positions=[dict(inst=k, strategy=p['strategy'], strategy_name=STRATEGIES[p['strategy']]['name'], side=p['side'], entry=round(p['entry'], 8),
                                     stop=round(p['stop'], 8), target=None if p['tp'] is None else round(p['tp'], 8), opened=iso(p['opened']), leverage=p['leverage'],
                                     qty=p['qty'], margin=round(p['margin'], 6), **position_mark(p, (prices or {}).get(k))) for k, p in sorted(acct['positions'].items())],
                pending=sorted(acct['pending']),
                pending_detail=[dict(inst=k, strategy=o['strategy'], strategy_name=STRATEGIES[o['strategy']]['name'], side=o['side']) for k, o in sorted(acct['pending'].items())])


RULES = '三套策略共用一个账户：1H 均值回归（RSI 25 / 趋势 2%，止损 1.5×ATR，止盈中轨，最长 24 小时）、1H 趋势突破只做多（48 小时新高，2×ATR 止损，3×ATR 跟随）、日线趋势突破只做多（20 日新高，2×ATR 止损，3×ATR 跟随）。共享余额与风控，一个币同时只持一个仓。'


def downsample(points, limit=720):
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def write_dashboard(data_dir, summary):
    """One compact public file for the web dashboard: summary, equity curve, open positions, recent trades."""
    accounts = {}
    for key, s in summary['accounts'].items():
        curve = []
        eq_path = data_dir / key / 'equity.csv'
        if eq_path.exists():
            with eq_path.open(newline='') as f:
                curve = [[int(datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')).timestamp() * 1000), float(r['equity'])] for r in csv.DictReader(f)]
        trades = read_trades(data_dir / key / 'trades.csv')[-30:]
        accounts[key] = dict(s, rules=RULES, curve=downsample(curve),
                             trades=[dict(inst=t['inst'], strategy=t['strategy'], strategy_name=STRATEGIES.get(t['strategy'], {}).get('name', t['strategy']), side=int(t['side']),
                                          entry_time=t['entry_time'], exit_time=t['exit_time'], entry=float(t['entry']), exit=float(t['exit']), pnl=t['pnl'], r=t['r'],
                                          reason=t['reason']) for t in reversed(trades)])
    out = dict(schema_version=1, updated_at=summary['updated_at'], start_equity=START_EQUITY, accounts=accounts)
    (data_dir / 'dashboard.json').write_text(json.dumps(out, ensure_ascii=False, separators=(',', ':')))


def write_readme(data_dir, summary):
    s = summary['accounts'][ACCOUNT]
    lines = ['# 模拟盘数据（学习用途）', '', f"更新：{summary['updated_at']}（UTC）· 合并账户从 {START_EQUITY:.0f} USDT 起步 · 由 `tools/paper_trader.py` 自动生成", '',
             f"**权益 {s['equity']:.2f} USDT（{s['return_pct']:+.2f}%）· 最大回撤 {s['max_drawdown_pct']:.2f}% · 已平仓 {s['closed_trades']} · 持仓 {len(s['open_positions'])}**", '',
             '| 策略 | 已平仓 | 胜率 | 平均 R | 盈亏 USDT | 持仓 |', '|---|---|---|---|---|---|']
    for b in s['by_strategy']:
        lines.append(f"| {b['name']} | {b['closed_trades']} | {'—' if b['win_rate_pct'] is None else str(b['win_rate_pct']) + '%'} | "
                     f"{'—' if b['avg_r'] is None else b['avg_r']} | {b['pnl']:+.2f} | {b['positions']} |")
    lines += ['', '仅为模拟，不下单、不构成投资建议。规则见 `tools/paper_trader.py` 文件头。', '']
    (data_dir / 'README.md').write_text('\n'.join(lines))


def run_once(data_dir, fetch=fetch_okx, until=None, now=None, backfill_days=0, reset=False, fetch_opens=None, fetch_prices=None):
    data_dir = Path(data_dir); data_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    if reset:  # start over: remove all account state and outputs (the branch history keeps the old data)
        import shutil
        for key in [ACCOUNT, *STRATEGIES]:
            shutil.rmtree(data_dir / key, ignore_errors=True)
        for name in ('summary.json', 'dashboard.json', 'README.md'):
            (data_dir / name).unlink(missing_ok=True)
    backfill_days = max(0, min(int(backfill_days or 0), 90))
    start_ts = int((now - timedelta(days=backfill_days)).timestamp() * 1000) if backfill_days else None
    if fetch_opens is None and fetch is fetch_okx:
        fetch_opens = fetch_open
    d = data_dir / ACCOUNT; d.mkdir(exist_ok=True)
    acct = load_state(d / 'state.json')
    acct['universe'] = list(INSTRUMENTS)
    acct['profile_mode'] = volume_profile(now)['mode']
    # After expiry, keep managing test-only positions and pending fills until they exit.
    managed = sorted(set(INSTRUMENTS) | set(acct['positions']) | set(acct['pending']))
    books = {}
    for bar in ('1H', '1Dutc'):
        # A backfill needs the replayed days plus 500 bars of indicator warm-up.
        pages = -(-(backfill_days * 24 + 500) // 100) if backfill_days and bar == '1H' else 3 if fetch is fetch_okx and len(INSTRUMENTS) >= 30 else None
        def get_series(inst):
            return fetch(inst, bar, **({'pages': pages} if pages else {}))
        if fetch is fetch_okx and len(managed) >= 30:
            with ThreadPoolExecutor(max_workers=4) as pool:
                series = dict(zip(managed, pool.map(get_series, managed)))
        else:
            series = {inst: get_series(inst) for inst in managed}
        series = {k: v for k, v in series.items() if v}
        if until is not None:
            series = {k: [b for b in v if b['ts'] <= until] for k, v in series.items()}
        opens = {}
        if fetch_opens and bar == '1H' and until is None:
            keys = list(series)
            if fetch is fetch_okx and len(keys) >= 30:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    opens = dict(zip(keys, pool.map(lambda inst: fetch_opens(inst, bar), keys)))
            else:
                opens = {inst: fetch_opens(inst, bar) for inst in keys}
        books[bar] = (series, {inst: indicators(bars) for inst, bars in series.items()}, opens)
    fresh = acct['last_ts'] is None
    trades, points, events = replay(acct, books, start_ts=start_ts if fresh else None)
    if fresh and acct['last_ts'] is not None:
        acct['started'] = iso(points[0][0])
        acct['live_since'] = now.isoformat(timespec='seconds').replace('+00:00', 'Z') if start_ts else acct['started']
    if trades:
        append_csv(d / 'trades.csv', TRADE_FIELDS, trades)
    if points:
        append_csv(d / 'equity.csv', ['timestamp', 'equity'], [dict(timestamp=iso(t), equity=v) for t, v in points])
    if events:
        with (d / 'events.jsonl').open('a') as f:
            f.writelines(json.dumps(e, ensure_ascii=False) + '\n' for e in events)
    atomic_json(d / 'state.json', acct)
    price_of = fetch_prices or (fetch_price if fetch is fetch_okx else None)
    prices = {k: price_of(k) for k in acct['positions']} if price_of and until is None else {}
    summary = dict(updated_at=now.isoformat(timespec='seconds').replace('+00:00', 'Z'), start_equity=START_EQUITY,
                   accounts={ACCOUNT: summarize(acct, data_dir, prices)})
    atomic_json(data_dir / 'summary.json', summary)
    write_readme(data_dir, summary)
    write_dashboard(data_dir, summary)
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', type=Path, required=True, help='directory holding account state and outputs')
    ap.add_argument('--reset', action='store_true', help='delete all account state and outputs before running')
    ap.add_argument('--backfill-days', type=int, default=0, help='start a fresh account this many days ago (max 90) and replay to now')
    args = ap.parse_args()
    p = volume_profile()
    universe = ranked_universe(okx_get('market/tickers?instType=SWAP'), okx_get('public/instruments?instType=SWAP'), p['scan_limit']) if p['mode'] == 'aggressive' else None
    print(describe_volume(configure_volume_test(universe=universe)), flush=True)
    s = run_once(args.data, backfill_days=args.backfill_days, reset=args.reset)['accounts'][ACCOUNT]
    print(f"{s['name']}: 权益 {s['equity']:.2f} USDT ({s['return_pct']:+.2f}%) · 已平仓 {s['closed_trades']} · 持仓 {len(s['open_positions'])} · 最新K线 {s['last_bar']}")
    for b in s['by_strategy']:
        print(f"  {b['name']}: 已平仓 {b['closed_trades']} · 盈亏 {b['pnl']:+.2f} · 持仓 {b['positions']}")


if __name__ == '__main__':
    main()
