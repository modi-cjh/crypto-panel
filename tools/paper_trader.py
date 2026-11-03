#!/usr/bin/env python3
"""Hourly paper trader for learning: replays newly closed OKX 1H candles through two rule sets.

It is a simulation only. It reads OKX public market data (no keys, no orders) and keeps two
independent 100 USDT paper accounts in a data directory:

  mean_reversion  -- entry rules of assets/indicators.mjs (mirror of bt60.engine.make_signal),
                     RSI 25 / trend 2%; stop 1.5xATR14 (0.5%..4%), target 20-bar mid band, 24h time stop.
  breakout_long   -- long-only Donchian: close > prior 48h high with EMA50 > EMA200; stop 2xATR14
                     (0.5%..4%), 3xATR chandelier trailing stop, 168h time stop, at most 6 positions.

Shared rules (identical to the research backtests): signals on closed bars, fill at the next bar's
open, 1% equity risk per trade, isolated 10x/5x by trailing 24h turnover (>= 50M USDT), same-direction
open risk <= 5%, one position per coin, daily (UTC+7) 8% loss halt, taker 0.05% + 0.02% slippage per
side, stop assumed first when stop and target fall inside one bar, loss capped at posted margin.

Each run only processes bars newer than the saved state, so reruns are idempotent and missed hours
are caught up from the fetched history. Standard library only.
"""
import argparse
import csv
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

COINS = 'BTC ETH SOL XRP DOGE BNB ADA LINK AVAX LTC DOT TRX BCH UNI NEAR AAVE ETC FIL APT ARB OP SUI PEPE'.split()
INSTRUMENTS = [f'{c}-USDT-SWAP' for c in COINS]
HOUR = 3_600_000
FEE, SLIP = 0.0005, 0.0002
START_EQUITY = 100.0
STRATEGIES = {
    'mean_reversion': dict(name='均值回归', kind='mr', rsi=25, trend=.02, stop_k=1.5, trail_k=None, hold=24, max_pos=None, long_only=False),
    'breakout_long': dict(name='趋势突破只做多', kind='donchian', lookback=48, stop_k=2.0, trail_k=3.0, hold=168, max_pos=6, long_only=True),
}
RISK, SAME_SIDE_CAP, DAILY_HALT = .01, .05, .08
UTC7 = timezone(timedelta(hours=7))


# ---------------------------------------------------------------- market data
def okx_get(path, retries=5):
    for attempt in range(retries):
        try:
            out = subprocess.run(['curl', '-sS', '-m', '20', 'https://www.okx.com/api/v5/' + path],
                                 capture_output=True, text=True, check=True).stdout
            body = json.loads(out)
            if body.get('code') == '0':
                return body['data']
        except (subprocess.CalledProcessError, ValueError):
            pass
        time.sleep(1 + attempt)
    raise RuntimeError(f'OKX request failed: {path}')


def fetch_okx(inst, pages=5):
    """Closed 1H bars, oldest first: dicts with ts/o/h/l/c/q (q = quote turnover)."""
    rows, after = {}, ''
    for _ in range(pages):
        data = okx_get(f'market/history-candles?instId={inst}&bar=1H&limit=100' + (f'&after={after}' if after else ''))
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


def replay(acct, cfg, series, ind, until=None):
    """Advance one account over closed bars newer than acct['last_ts']. Returns (trades, equity_points, events)."""
    index = {inst: {b['ts']: i for i, b in enumerate(bars)} for inst, bars in series.items()}
    times = sorted({b['ts'] for bars in series.values() for b in bars})
    if until is not None:
        times = [t for t in times if t <= until]
    trades, points, events = [], [], []
    if acct['last_ts'] is None:  # start fresh from the latest closed bar: no backfilled history
        if times:
            acct['last_ts'] = times[-1]
            acct['day'], acct['day_start'] = local_day(times[-1]), acct['cash']
            points.append((times[-1], round(acct['cash'], 6)))
            events.append(dict(ts=iso(times[-1]), type='started', equity=acct['cash']))
        return trades, points, events
    new = [t for t in times if t > acct['last_ts']]
    if new and new[0] - acct['last_ts'] > HOUR:
        events.append(dict(ts=iso(new[0]), type='gap', missing_hours=(new[0] - acct['last_ts']) // HOUR - 1))
    last_close = dict(acct.get('last_marks', {}))
    for t in new:
        bars_now = {inst: series[inst][index[inst][t]] for inst in series if t in index[inst]}
        # 1) fill orders signalled on the previous bar at this bar's open
        for inst, o in sorted(acct['pending'].items()):
            if inst not in bars_now:
                continue
            del acct['pending'][inst]
            bar, s = bars_now[inst], o['side']
            entry = bar['o'] * (1 + SLIP * s)
            dist = min(max(cfg['stop_k'] * o['atr'] / entry, .005), .04)
            tp = o.get('mid')
            if tp is not None and (tp - entry) * s <= 0:
                events.append(dict(ts=iso(t), type='skip', inst=inst, reason='already_at_mean')); continue
            equity = mark(acct, last_close)
            same = sum(p['risk'] for p in acct['positions'].values() if p['side'] == s)
            if same + o['risk'] > SAME_SIDE_CAP * equity:
                events.append(dict(ts=iso(t), type='skip', inst=inst, reason='same_side_risk')); continue
            if cfg['max_pos'] is not None and len(acct['positions']) >= cfg['max_pos']:
                events.append(dict(ts=iso(t), type='skip', inst=inst, reason='max_positions')); continue
            qty = o['risk'] / (entry * dist)
            lev = 10 if o['turn24'] >= 50e6 else 5
            margin = qty * entry / lev
            if margin > equity - sum(p['margin'] for p in acct['positions'].values()):
                events.append(dict(ts=iso(t), type='skip', inst=inst, reason='margin')); continue
            acct['positions'][inst] = dict(side=s, entry=entry, stop=entry * (1 - s * dist), tp=tp, qty=qty, margin=margin,
                                           risk=o['risk'], leverage=lev, opened=t, bars=0)
            events.append(dict(ts=iso(t), type='trade_opened', inst=inst, side=s, price=round(entry, 8), stop=round(entry * (1 - s * dist), 8),
                               target=None if tp is None else round(tp, 8), leverage=lev))
        # 2) manage open positions on this bar
        for inst, p in sorted(acct['positions'].items()):
            if inst not in bars_now:
                continue
            bar, s = bars_now[inst], p['side']
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
                row = dict(strategy=cfg['kind'], inst=inst, side=s, entry_time=iso(p['opened']), exit_time=iso(t), entry=round(p['entry'], 8),
                           exit=round(ex, 8), qty=p['qty'], leverage=p['leverage'], pnl=round(gross - fee, 6), r=round((gross - fee) / p['risk'], 4), reason=reason)
                trades.append(row); events.append(dict(ts=iso(t), type='trade_closed', inst=inst, side=s, price=round(ex, 8), pnl=row['pnl'], reason=reason))
                del acct['positions'][inst]
            else:
                p['bars'] += 1
                if cfg['trail_k']:
                    a = ind[inst]['atr'][index[inst][t]]
                    p['stop'] = max(p['stop'], bar['c'] - cfg['trail_k'] * a) if s == 1 else min(p['stop'], bar['c'] + cfg['trail_k'] * a)
        # 3) mark equity, daily halt
        last_close.update({inst: b['c'] for inst, b in bars_now.items()})
        equity = mark(acct, last_close)
        if local_day(t) != acct['day']:
            acct['day'], acct['day_start'], acct['halted'] = local_day(t), equity, False
        if not acct['halted'] and equity <= acct['day_start'] * (1 - DAILY_HALT):
            acct['halted'] = True; events.append(dict(ts=iso(t), type='daily_halt', equity=round(equity, 6)))
        points.append((t, round(equity, 6)))
        acct['last_ts'] = t
        # 4) new signals on this closed bar fill at the next bar's open
        if acct['halted']:
            continue
        for inst in sorted(bars_now):
            if inst in acct['positions'] or inst in acct['pending']:
                continue
            i = index[inst][t]; s = signal(ind[inst], i, cfg)
            if s == 0 or (cfg['long_only'] and s == -1):
                continue
            turn24 = sum(ind[inst]['q'][max(0, i - 23):i + 1])
            acct['pending'][inst] = dict(side=s, atr=ind[inst]['atr'][i], mid=ind[inst]['mid'][i] if cfg['kind'] == 'mr' else None,
                                         turn24=turn24, risk=RISK * equity, signal_ts=t)
            events.append(dict(ts=iso(t), type='signal', inst=inst, side=s))
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


def summarize(key, cfg, acct, data_dir):
    trades = read_trades(data_dir / key / 'trades.csv')
    equity = mark(acct, acct.get('last_marks', {}))
    wins = [t for t in trades if t['pnl'] > 0]; losses = [t for t in trades if t['pnl'] <= 0]
    peak, dd = START_EQUITY, 0.0
    eq_path = data_dir / key / 'equity.csv'
    if eq_path.exists():
        with eq_path.open(newline='') as f:
            for r in csv.DictReader(f):
                v = float(r['equity']); peak = max(peak, v); dd = max(dd, (peak - v) / peak * 100)
    return dict(name=cfg['name'], equity=round(equity, 4), return_pct=round((equity / START_EQUITY - 1) * 100, 2), max_drawdown_pct=round(dd, 2),
                closed_trades=len(trades), win_rate_pct=round(len(wins) / len(trades) * 100, 1) if trades else None,
                avg_r=round(sum(t['r'] for t in trades) / len(trades), 3) if trades else None,
                payoff=round((sum(t['pnl'] for t in wins) / len(wins)) / (-sum(t['pnl'] for t in losses) / len(losses)), 2) if wins and losses and sum(t['pnl'] for t in losses) < 0 else None,
                halted=acct['halted'], last_bar=iso(acct['last_ts']) if acct['last_ts'] else None,
                open_positions=[dict(inst=k, side=p['side'], entry=round(p['entry'], 8), stop=round(p['stop'], 8), target=None if p['tp'] is None else round(p['tp'], 8),
                                     opened=iso(p['opened']), leverage=p['leverage']) for k, p in sorted(acct['positions'].items())],
                pending=sorted(acct['pending']))


def write_readme(data_dir, summary):
    lines = ['# 模拟盘数据（学习用途）', '', f"更新：{summary['updated_at']}（UTC）· 两个账户各从 {START_EQUITY:.0f} USDT 起步 · 由 `tools/paper_trader.py` 每小时自动生成", '',
             '| 账户 | 权益 USDT | 收益 | 最大回撤 | 已平仓 | 胜率 | 平均 R | 持仓 |', '|---|---|---|---|---|---|---|---|']
    for s in summary['accounts'].values():
        lines.append(f"| {s['name']} | {s['equity']:.2f} | {s['return_pct']:+.2f}% | {s['max_drawdown_pct']:.2f}% | {s['closed_trades']} | "
                     f"{'—' if s['win_rate_pct'] is None else str(s['win_rate_pct']) + '%'} | {'—' if s['avg_r'] is None else s['avg_r']} | {len(s['open_positions'])} |")
    lines += ['', '仅为模拟，不下单、不构成投资建议。规则见 `tools/paper_trader.py` 文件头。', '']
    (data_dir / 'README.md').write_text('\n'.join(lines))


def run_once(data_dir, fetch=fetch_okx, until=None, now=None):
    data_dir = Path(data_dir); data_dir.mkdir(parents=True, exist_ok=True)
    series = {inst: fetch(inst) for inst in INSTRUMENTS}
    series = {k: v for k, v in series.items() if v}
    if until is not None:
        series = {k: [b for b in v if b['ts'] <= until] for k, v in series.items()}
    ind = {inst: indicators(bars) for inst, bars in series.items()}
    accounts = {}
    for key, cfg in STRATEGIES.items():
        d = data_dir / key; d.mkdir(exist_ok=True)
        acct = load_state(d / 'state.json')
        trades, points, events = replay(acct, cfg, series, ind)
        if trades:
            append_csv(d / 'trades.csv', TRADE_FIELDS, trades)
        if points:
            append_csv(d / 'equity.csv', ['timestamp', 'equity'], [dict(timestamp=iso(t), equity=v) for t, v in points])
        if events:
            with (d / 'events.jsonl').open('a') as f:
                f.writelines(json.dumps(e, ensure_ascii=False) + '\n' for e in events)
        (d / 'state.json').write_text(json.dumps(acct, ensure_ascii=False, indent=1, sort_keys=True))
        accounts[key] = summarize(key, cfg, acct, data_dir)
    summary = dict(updated_at=(now or datetime.now(timezone.utc)).isoformat(timespec='seconds').replace('+00:00', 'Z'), start_equity=START_EQUITY, accounts=accounts)
    (data_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    write_readme(data_dir, summary)
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', type=Path, required=True, help='directory holding account state and outputs')
    args = ap.parse_args()
    summary = run_once(args.data)
    for s in summary['accounts'].values():
        print(f"{s['name']}: 权益 {s['equity']:.2f} USDT ({s['return_pct']:+.2f}%) · 已平仓 {s['closed_trades']} · 持仓 {len(s['open_positions'])} · 最新K线 {s['last_bar']}")


if __name__ == '__main__':
    main()
