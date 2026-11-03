import csv
import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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
    def __call__(self, inst, bar='1H'):
        if inst != 'BTC-USDT-SWAP' or bar != '1H': return []
        return [r for r in self.rows if self.until is None or r['ts'] <= self.until][-500:]


def run(tmp, feed, until):
    feed.until = until
    return pt.run_once(tmp, fetch=feed)


def trades(tmp, key=None):
    """Closed trades of the shared account, optionally only those made by one strategy."""
    path = Path(tmp) / pt.ACCOUNT / 'trades.csv'
    rows = list(csv.DictReader(path.read_text().splitlines())) if path.exists() else []
    return [r for r in rows if key is None or r['strategy'] == key]


def events_of(tmp, key=None):
    lines = (Path(tmp) / pt.ACCOUNT / 'events.jsonl').read_text().splitlines()
    return [e for e in map(json.loads, lines) if key is None or e.get('strategy') == key]


def state(tmp):
    return json.loads((Path(tmp) / pt.ACCOUNT / 'state.json').read_text())


DAY = 24 * H


class DailyFeed:
    """Serves daily rows for 1Dutc and matching hourly bars (flat at the open, closing at the day's close) for 1H."""
    def __init__(self, rows):
        self.rows, self.until = rows, None
        self.hours = []
        for d in rows:
            for h in range(24):
                o = d['o'] if h < 23 else d['o']; c = d['c'] if h == 23 else d['o']
                self.hours.append(dict(ts=d['ts'] + h * H, o=o, h=max(o, c), l=min(o, c), c=c, q=d['q'] / 24))
    def __call__(self, inst, bar='1H'):
        if inst != 'BTC-USDT-SWAP': return []
        if bar == '1Dutc':  # a daily bar is closed once its last hour is
            return [r for r in self.rows if self.until is None or r['ts'] + 23 * H <= self.until][-500:]
        return [r for r in self.hours if self.until is None or r['ts'] <= self.until][-500:]


def volatile_daily_breakout():
    """Daily uptrend with wide ranges (ATR about 10% of price), then a 20-day breakout, fill, and a crash."""
    rows, px = [], 100.0
    for i in range(260):
        o, c = px, px * (1.002 if i % 2 else 0.999)
        rows.append(dict(ts=T0 + i * DAY, o=o, h=max(o, c) * 1.05, l=min(o, c) * 0.95, c=c, q=1e9)); px = c
    n = len(rows); top = max(r['h'] for r in rows[-20:]); c = top * 1.01
    rows.append(dict(ts=T0 + n * DAY, o=px, h=c * 1.01, l=px * 0.99, c=c, q=1e9))
    rows.append(dict(ts=T0 + (n + 1) * DAY, o=c, h=c * 1.01, l=c * 0.99, c=c, q=1e9))
    rows.append(dict(ts=T0 + (n + 2) * DAY, o=c * 0.98, h=c * 0.98, l=c * 0.6, c=c * 0.62, q=1e9))
    return rows


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
        events = events_of(self.tmp, 'breakout_long')
        self.assertEqual([e['type'] for e in events if e['type'] in ('signal', 'trade_opened', 'trade_closed')], ['signal', 'trade_opened', 'trade_closed'])

    def test_incremental_hourly_runs_match_a_single_catch_up_run(self):
        rows = breakout_then([(1.0, 1.03, 0.995, 1.02), (1.02, 1.05, 1.01, 1.04), (1.04, 1.04, 0.97, 0.975)])
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        start = rows[-7]['ts']
        fa, fb = Feed(rows), Feed(rows)
        run(a, fa, start); run(b, fb, start)
        for r in rows[-6:]: run(a, fa, r['ts'])
        run(b, fb, rows[-1]['ts'])  # one run catching up six hours
        self.assertEqual(state(a), state(b))
        self.assertEqual(trades(a), trades(b))
        t = trades(a, 'breakout_long')
        self.assertEqual(len(t), 1); self.assertEqual(t[0]['reason'], 'stop')  # trailing stop locked in part of the move
        self.assertGreater(float(t[0]['pnl']), 0)

    def test_gap_is_logged_and_daily_halt_blocks_new_signals(self):
        acct = pt.new_account(); acct.update(last_ts=T0, day=pt.local_day(T0), day_start=100.0)
        acct['positions']['BTC-USDT-SWAP'] = dict(strategy='breakout_long', side=1, entry=100.0, stop=50.0, tp=None, qty=1.0, margin=10.0, risk=1.0, leverage=10, opened=T0, bars=0)
        rows = [bar(0, 100, 100, 100, 100), bar(3, 85, 85, 85, 85)]  # -15 move, but isolated loss stops at the margin
        series = {'BTC-USDT-SWAP': rows}; ind = {'BTC-USDT-SWAP': pt.indicators(rows)}
        _, points, events = pt.replay(acct, {'1H': (series, ind, {})})
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

    def test_daily_breakout_uses_wide_stop_and_lowers_leverage_to_stay_beyond_liquidation(self):
        rows = volatile_daily_breakout(); feed = DailyFeed(rows)
        close = lambda r: r['ts'] + 23 * H            # the hour that completes this UTC day
        order = pt.ORDER; pt.ORDER = ['breakout_daily']  # isolate the daily rule from hourly signals on synthetic hours
        try:
            run(self.tmp, feed, close(rows[-4]))
            for r in rows[-3:]: run(self.tmp, feed, close(r))
        finally:
            pt.ORDER = order
        t = trades(self.tmp, 'breakout_daily')
        self.assertEqual(len(t), 1); t = t[0]
        self.assertEqual(t['reason'], 'stop')
        entry = float(t['entry']); stop_dist = 1 - float(t['exit']) / entry  # gap down fills below the stop
        self.assertGreater(stop_dist, .04)                       # wider than the 1H cap
        self.assertLess(int(t['leverage']), 10)                  # lowered so liquidation sits beyond the stop
        self.assertLessEqual(int(t['leverage']) * (stop_dist if stop_dist < .25 else .25), 1.0)
        self.assertGreater(float(t['pnl']), -float(t['qty']) * entry / int(t['leverage']) - 1e-9)  # never beyond margin
        self.assertEqual(t['entry_time'], pt.iso(rows[-2]['ts']))  # filled at the next UTC day's open
        s = json.loads((Path(self.tmp) / 'summary.json').read_text())
        self.assertEqual(list(s['accounts']), [pt.ACCOUNT])
        acct = s['accounts'][pt.ACCOUNT]
        self.assertEqual([b['key'] for b in acct['by_strategy']], ['mean_reversion', 'breakout_long', 'breakout_daily'])
        self.assertEqual(acct['by_strategy'][2]['closed_trades'], 1)
        dash = json.loads((Path(self.tmp) / 'dashboard.json').read_text())['accounts'][pt.ACCOUNT]
        self.assertTrue(dash['rules'])
        self.assertEqual(len(dash['trades']), 1); self.assertEqual(dash['trades'][0]['strategy_name'], '日线趋势突破')
        self.assertEqual(len(dash['curve']), 1 + 3 * 24); self.assertTrue(all(isinstance(ts, int) and v > 0 for ts, v in dash['curve']))

    def test_dashboard_lists_open_positions_with_size_and_downsamples_long_curves(self):
        rows = breakout_then([(1.0, 1.01, 0.995, 1.005)]); feed = Feed(rows)
        run(self.tmp, feed, rows[-4]['ts'])
        for r in rows[-3:]: run(self.tmp, feed, r['ts'])
        pos = json.loads((Path(self.tmp) / 'dashboard.json').read_text())['accounts'][pt.ACCOUNT]['open_positions']
        self.assertEqual(len(pos), 1); self.assertGreater(pos[0]['qty'], 0); self.assertGreater(pos[0]['margin'], 0)
        pts = [[i, float(i)] for i in range(5000)]
        small = pt.downsample(pts, 720)
        self.assertEqual(len(small), 720); self.assertEqual(small[0], pts[0]); self.assertEqual(small[-1], pts[-1])

    def test_backfill_replays_past_days_like_hourly_runs_then_marks_live_since(self):
        rows = breakout_then([(1.0, 1.03, 0.995, 1.02), (1.02, 1.05, 1.01, 1.04), (1.04, 1.04, 0.97, 0.975)])
        class KwFeed(Feed):
            def __call__(self, inst, bar='1H', **kw): return super().__call__(inst, bar)
        last = rows[-1]['ts']; now = datetime.fromtimestamp((last + H // 2) / 1000, timezone.utc)
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        s = pt.run_once(a, fetch=KwFeed(rows), now=now, backfill_days=1)  # one run replays the last day
        acct = s['accounts'][pt.ACCOUNT]
        self.assertEqual(acct['live_since'], now.isoformat(timespec='seconds').replace('+00:00', 'Z'))
        self.assertLess(acct['started'], acct['live_since'])
        anchor = max(r['ts'] for r in rows if r['ts'] <= last + H // 2 - 24 * H)
        self.assertEqual(acct['started'], pt.iso(anchor))
        feed = Feed(rows); run(b, feed, anchor)                     # the same start, advanced hour by hour
        for r in rows:
            if r['ts'] > anchor: run(b, feed, r['ts'])
        self.assertEqual(trades(a, 'breakout_long'), trades(b, 'breakout_long'))
        self.assertEqual(len(trades(a, 'breakout_long')), 1)
        events = events_of(a)
        self.assertTrue(events[0]['backfill'])

    def test_reset_clears_accounts_and_backfill_only_applies_to_fresh_accounts(self):
        rows = uptrend(); feed = Feed(rows)
        run(self.tmp, feed, rows[-3]['ts']); run(self.tmp, feed, rows[-1]['ts'])
        before = (Path(self.tmp) / pt.ACCOUNT / 'equity.csv').read_text()
        class KwFeed(Feed):
            def __call__(self, inst, bar='1H', **kw): return super().__call__(inst, bar)
        now = datetime.fromtimestamp((rows[-1]['ts'] + H) / 1000, timezone.utc)
        pt.run_once(self.tmp, fetch=KwFeed(rows), now=now, backfill_days=5)   # existing state: backfill ignored
        self.assertEqual((Path(self.tmp) / pt.ACCOUNT / 'equity.csv').read_text(), before)
        s = pt.run_once(self.tmp, fetch=KwFeed(rows), now=now, reset=True, backfill_days=2)
        eq = (Path(self.tmp) / pt.ACCOUNT / 'equity.csv').read_text().splitlines()
        self.assertEqual(len(eq) - 1, 2 * 24)                         # header + one point per replayed hour (start included)
        self.assertEqual(s['accounts'][pt.ACCOUNT]['equity'], 100.0)

    def test_pending_order_fills_as_soon_as_the_next_bar_opens_with_identical_results(self):
        rows = breakout_then([(1.0, 1.03, 0.995, 1.02), (1.02, 1.05, 1.01, 1.04), (1.04, 1.04, 0.97, 0.975)])
        sig, fill_bar = rows[-5], rows[-4]                       # signal bar, then the bar that fills at its open
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        fa, fb = Feed(rows), Feed(rows)
        run(a, fa, rows[-6]['ts']); run(b, fb, rows[-6]['ts'])
        run(a, fa, sig['ts']); run(b, fb, sig['ts'])           # signal recorded, order pending
        fa.until = sig['ts']                                    # a: the fill bar has opened but not closed yet
        s = pt.run_once(a, fetch=fa, fetch_opens=lambda inst, bar: dict(ts=fill_bar['ts'], o=fill_bar['o']) if inst == 'BTC-USDT-SWAP' and bar == '1H' else None)
        pos = s['accounts'][pt.ACCOUNT]['open_positions']
        self.assertEqual(len(pos), 1); self.assertEqual(pos[0]['opened'], pt.iso(fill_bar['ts']))
        self.assertEqual(s['accounts'][pt.ACCOUNT]['pending'], [])
        self.assertEqual(s['accounts'][pt.ACCOUNT]['last_bar'], pt.iso(sig['ts']))  # no unfinished bar is marked
        pt.run_once(a, fetch=fa, fetch_opens=lambda inst, bar: dict(ts=fill_bar['ts'], o=fill_bar['o']))  # rerun: no double fill
        for r in rows[-4:]: run(a, fa, r['ts']); run(b, fb, r['ts'])
        self.assertEqual(trades(a, 'breakout_long'), trades(b, 'breakout_long'))
        self.assertEqual(state(a), state(b))
        opened = [e for e in events_of(a) if e['type'] == 'trade_opened']
        self.assertEqual(len(opened), 1)

    def test_run_records_the_latest_price_and_floating_pnl_of_open_positions(self):
        rows = breakout_then([(1.0, 1.01, 0.995, 1.005)]); feed = Feed(rows)
        run(self.tmp, feed, rows[-4]['ts'])
        for r in rows[-3:]: run(self.tmp, feed, r['ts'])
        pos = json.loads((Path(self.tmp) / 'summary.json').read_text())['accounts'][pt.ACCOUNT]['open_positions'][0]
        self.assertNotIn('mark', pos)                                    # no price source in this feed
        s = pt.run_once(self.tmp, fetch=feed, fetch_prices=lambda inst: pos['entry'] * 1.01)
        p = s['accounts'][pt.ACCOUNT]['open_positions'][0]
        self.assertAlmostEqual(p['mark'], pos['entry'] * 1.01)
        self.assertAlmostEqual(p['unrealized'], pos['qty'] * pos['entry'] * .01, places=5)
        low = pt.run_once(self.tmp, fetch=feed, fetch_prices=lambda inst: pos['entry'] * .5)['accounts'][pt.ACCOUNT]['open_positions'][0]
        self.assertAlmostEqual(low['unrealized'], -pos['margin'], places=5)  # capped at the posted margin
        dash = json.loads((Path(self.tmp) / 'dashboard.json').read_text())['accounts'][pt.ACCOUNT]['open_positions'][0]
        self.assertIn('unrealized', dash)
        acct = s['accounts'][pt.ACCOUNT]
        self.assertAlmostEqual(acct['cash'] + p['unrealized'], acct['cash'] + pos['qty'] * pos['entry'] * .01, places=5)
        self.assertIn('cash', dash and json.loads((Path(self.tmp) / 'dashboard.json').read_text())['accounts'][pt.ACCOUNT])


if __name__ == '__main__':
    unittest.main()
