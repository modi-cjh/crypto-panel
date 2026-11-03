import csv
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import paper_trader as pt

H = pt.HOUR
T0 = 1_780_000_000_000 // H * H


def bar(i, o, h, l, c, q=1e8):
    return dict(ts=T0 + i * H, o=o, h=h, l=l, c=c, q=q)


def uptrend(n=260):
    """Gentle uptrend with a small zig-zag so EMA50 > EMA200 and no 48h breakout until the end."""
    rows, px = [], 100.0
    for i in range(n):
        drift = 1.0004 if i % 2 else 0.9999
        o, c = px, px * drift
        rows.append(bar(i, o, max(o, c) * 1.001, min(o, c) * 0.999, c)); px = c
    return rows


def breakout_then(after):
    """Uptrend, then a breakout bar (signal), the fill bar, then the given follow-up bars (o, h, l, c ratios of fill open)."""
    rows = uptrend()
    last = rows[-1]['c']; n = len(rows)
    top = max(r['h'] for r in rows[-48:])
    c = top * 1.01
    rows.append(bar(n, last, c * 1.001, last * 0.999, c))  # signal bar closes above the prior 48h high
    fill = c
    rows.append(bar(n + 1, fill, fill * 1.002, fill * 0.998, fill))
    for k, (o, h, l, cl) in enumerate(after):
        rows.append(bar(n + 2 + k, fill * o, fill * h, fill * l, fill * cl))
    return rows


class Feed:
    def __init__(self, rows): self.rows, self.until = rows, None
    def __call__(self, inst):
        if inst != 'BTC-USDT-SWAP': return []
        return [r for r in self.rows if self.until is None or r['ts'] <= self.until][-500:]


def run(tmp, feed, until):
    feed.until = until
    return pt.run_once(tmp, fetch=feed)


def trades(tmp, key):
    path = Path(tmp) / key / 'trades.csv'
    return list(csv.DictReader(path.read_text().splitlines())) if path.exists() else []


class PaperTraderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_first_run_starts_fresh_and_reruns_are_idempotent(self):
        rows = uptrend(); feed = Feed(rows)
        s = run(self.tmp, feed, rows[-1]['ts'])
        for acct in s['accounts'].values():
            self.assertEqual(acct['equity'], 100.0); self.assertEqual(acct['closed_trades'], 0)
        before = {p.name: p.read_bytes() for p in Path(self.tmp).rglob('*') if p.is_file() and p.name != 'summary.json' and p.name != 'README.md'}
        run(self.tmp, feed, rows[-1]['ts'])
        after = {p.name: p.read_bytes() for p in Path(self.tmp).rglob('*') if p.is_file() and p.name != 'summary.json' and p.name != 'README.md'}
        self.assertEqual(before, after)

    def test_breakout_fills_next_open_and_stop_caps_loss_near_one_r(self):
        rows = breakout_then([(1.0, 1.0, 0.90, 0.91)])  # crash through the stop on the bar after the fill
        feed = Feed(rows)
        run(self.tmp, feed, rows[-4]['ts'])  # start just before the signal bar
        for r in rows[-3:]: run(self.tmp, feed, r['ts'])
        t = trades(self.tmp, 'breakout_long')
        self.assertEqual(len(t), 1); t = t[0]
        self.assertEqual(t['reason'], 'stop'); self.assertEqual(int(t['side']), 1)
        fill_open = rows[-2]['o']
        self.assertAlmostEqual(float(t['entry']), fill_open * (1 + pt.SLIP), places=6)
        self.assertEqual(t['entry_time'], pt.iso(rows[-2]['ts']))
        # Loss is about 1R (1% of 100) plus fees, never more than the posted margin.
        self.assertTrue(-1.3 < float(t['pnl']) < -0.9, t['pnl'])
        events = [json.loads(l) for l in (Path(self.tmp) / 'breakout_long' / 'events.jsonl').read_text().splitlines()]
        self.assertEqual([e['type'] for e in events if e['type'] in ('signal', 'trade_opened', 'trade_closed')], ['signal', 'trade_opened', 'trade_closed'])

    def test_incremental_hourly_runs_match_a_single_catch_up_run(self):
        rows = breakout_then([(1.0, 1.03, 0.995, 1.02), (1.02, 1.05, 1.01, 1.04), (1.04, 1.04, 0.97, 0.975)])
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        start = rows[-7]['ts']
        fa, fb = Feed(rows), Feed(rows)
        run(a, fa, start); run(b, fb, start)
        for r in rows[-6:]: run(a, fa, r['ts'])
        run(b, fb, rows[-1]['ts'])  # one run catching up six hours
        for key in pt.STRATEGIES:
            sa = json.loads((Path(a) / key / 'state.json').read_text()); sb = json.loads((Path(b) / key / 'state.json').read_text())
            self.assertEqual(sa, sb, key)
            self.assertEqual(trades(a, key), trades(b, key))
        t = trades(a, 'breakout_long')
        self.assertEqual(len(t), 1); self.assertEqual(t[0]['reason'], 'stop')  # trailing stop locked in part of the move
        self.assertGreater(float(t[0]['pnl']), 0)

    def test_gap_is_logged_and_daily_halt_blocks_new_signals(self):
        acct = pt.new_account(); acct.update(last_ts=T0, day=pt.local_day(T0), day_start=100.0)
        acct['positions']['BTC-USDT-SWAP'] = dict(side=1, entry=100.0, stop=50.0, tp=None, qty=1.0, margin=10.0, risk=1.0, leverage=10, opened=T0, bars=0)
        rows = [bar(0, 100, 100, 100, 100), bar(3, 85, 85, 85, 85)]  # -15 move, but isolated loss stops at the margin
        series = {'BTC-USDT-SWAP': rows}; ind = {'BTC-USDT-SWAP': pt.indicators(rows)}
        _, points, events = pt.replay(acct, pt.STRATEGIES['breakout_long'], series, ind)
        types = [e['type'] for e in events]
        self.assertIn('gap', types)
        self.assertEqual(points[-1][1], 90.0)  # loss capped at the 10 USDT margin
        self.assertIn('daily_halt', types); self.assertTrue(acct['halted'])

    def test_mean_reversion_signal_matches_the_dashboard_rules(self):
        cfg = pt.STRATEGIES['mean_reversion']
        self.assertEqual((cfg['rsi'], cfg['trend'], cfg['stop_k'], cfg['hold']), (25, .02, 1.5, 24))
        x = pt.indicators(uptrend())
        self.assertTrue(all(pt.signal(x, i, cfg) == 0 for i in range(len(x['c']))))  # calm trend: no reversion entries
        self.assertTrue(math.isclose(x['e50'][0], uptrend()[0]['c']))  # EMA seeds with the first close


if __name__ == '__main__':
    unittest.main()
