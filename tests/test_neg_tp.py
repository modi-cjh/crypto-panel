"""NEGATIVE TP RULE (2026-11-25, user: 「负数止盈时不挂止盈，只靠止损和跟踪止盈出场」).

When entry ± target_rr × dist is not a valid take-profit price (≤ 0 — wide daily shorts on H / MAGIC / INJ / HOME — or not
beyond the entry), the position still opens, with NO take-profit, and exits by its stop / trailing stop / reverse signal /
paper close only.

- execution: take_profit() → None; simulate / simulate_account (incl. the exit_rules_from upgrade) / the PRICE_OFF LIVE
  virtual positions store target None, and the results are otherwise IDENTICAL to the old negative-target arithmetic
  (a target ≤ 0 can never fill on positive prices), so no cutover key is needed;
- paper: signals.json carries tp None + no_target True for those (ordinary, pending and price_off_live);
- okx_demo_trader: opens with the stop only (no tpTriggerPx), never re-adds / amends a TP, classifies an exchange close as
  stop / trailing stop, and old signals.json (negative tp float, legacy tp None) never sends a TP ≤ 0.
Nothing here reaches OKX: fake clients / a fake curl only.
"""
import json
import math
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'evolve'))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import execution as X      # noqa: E402
import genome as G         # noqa: E402
import paper as P          # noqa: E402
import okx_demo_trader as odt  # noqa: E402
import fetch_signals       # noqa: E402
import live_sync as ls     # noqa: E402
from test_okx_demo_trader import FakeOKX, NOW, NOW_MS  # noqa: E402
from test_okx_live_mode import LiveModeBase  # noqa: E402
import test_price_off_live as POL  # noqa: E402
from test_price_off_live import H, T0, walk, series_from, LiveFakeOKX, candles, real_atr, pol_signal, doc  # noqa: E402

SL_ONLY = {'slTriggerPx', 'slOrdPx', 'slTriggerPxType'}
ORDER = '/api/v5/trade/order'


def old_take_profit(entry, side, rr, dist):
    """The arithmetic before this change: entry ± rr × dist, negative or not (None only for 'no gene target')."""
    return entry + side * rr * dist if rr and rr > 0 else None


def strip_targets(x):
    """A simulate / simulate_account result without its `target` fields (the only thing the rule may change)."""
    if isinstance(x, dict):
        return {k: strip_targets(v) for k, v in x.items() if k != 'target'}
    if isinstance(x, (list, tuple)):
        return type(x)(strip_targets(v) for v in x)
    return x


# ================================================================================================ execution
class TakeProfitHelperTests(unittest.TestCase):
    def test_valid_targets_are_the_old_value_bit_for_bit(self):
        rng = random.Random(7)
        for _ in range(2000):
            e, d, rr, dist = rng.uniform(.001, 1e5), rng.choice((1, -1)), rng.uniform(1, 6), rng.uniform(1e-6, 1) * 1e4
            tp = X.take_profit(e, d, rr, dist)
            raw = e + d * rr * dist
            if raw > 0:
                self.assertEqual(tp, raw)                   # exactly the same float, not just close
            else:
                self.assertIsNone(tp)

    def test_invalid_targets_are_none(self):
        self.assertIsNone(X.take_profit(0.0264, -1, 6.0, 0.007))     # H daily short: 0.0264 − 0.042 < 0
        self.assertIsNone(X.take_profit(100.0, -1, 2.0, 50.0))       # exactly 0
        self.assertIsNone(X.take_profit(100.0, 1, 0, 5.0))           # old 'no target' gene (old exits)
        self.assertIsNone(X.take_profit(100.0, 1, 2.0, 0.0))         # no distance: not beyond the entry
        self.assertIsNone(X.take_profit(100.0, 1, 2.0, -1.0))
        self.assertIsNone(X.take_profit(100.0, 1, 2.0, float('inf')))
        self.assertEqual(X.take_profit(100.0, -1, 2.0, 10.0), 80.0)
        self.assertEqual(X.take_profit(100.0, 1, 6.0, 1000.0), 6100.0)   # a long target is always valid

    def test_exit_checks_treat_none_as_no_target(self):
        p = dict(side=-1, stop=160.0, target=None, entry=100.0, dist=60.0)
        self.assertIsNone(X.gap_exit(p, 100.0))
        self.assertEqual(X.gap_exit(p, 161.0), (161.0, 'stop'))
        self.assertFalse(X.target_hit(p, 101.0, 0.0000001))
        # the old negative target could never fill either, on any positive price
        q = dict(p, target=-50.0)
        for o in (1e-9, 1.0, 99.0):
            self.assertIsNone(X.gap_exit(q, o)); self.assertFalse(X.target_hit(q, o, o))
        # trail_update ignores the target altogether
        a, b = dict(p, best=100.0), dict(q, best=100.0)
        X.trail_update(a, 100.0, 40.0); X.trail_update(b, 100.0, 40.0)
        self.assertEqual((a['stop'], a['trailing']), (b['stop'], b['trailing']))


class SimulateEquivalenceTests(unittest.TestCase):
    """Proof by replay: target None instead of a target ≤ 0 changes nothing but the stored `target` field."""

    def market(self, n=1200, coins=5):
        return {f'C{k}-USDT-SWAP': series_from([x / 100 for x in walk(n, 40 + k)], f'C{k}-USDT-SWAP', spread=.01) for k in range(coins)}

    @staticmethod
    def widen(real):
        """signal_sets with many short distances blown up so that target_rr × dist passes zero (and some stops do fill)."""
        def sets(g, series):
            ev, rv = real(g, series)
            rng = random.Random(int(g.get('lookback', 1)) * 31 + int(g.get('tf', 1)))
            wide = {i: [(inst, d, dist * (rng.choice((1, 8, 30, 80)) if d == -1 else 1)) for inst, d, dist in lst] for i, lst in ev.items()}
            return wide, rv
        return sets

    def runs(self, fn):
        """(new, old, number of targets the new rule suppressed)"""
        real = X.signal_sets
        none = []
        orig = X.take_profit

        def counting(*a):
            tp = orig(*a)
            if tp is None and old_take_profit(*a) is not None:
                none.append(a)
            return tp
        with mock.patch.object(X, 'signal_sets', side_effect=self.widen(real)):
            with mock.patch.object(X, 'take_profit', counting):
                new = fn()
            with mock.patch.object(X, 'take_profit', old_take_profit):
                old = fn()
        return new, old, len(none)

    def test_simulate_identical_trades_curve_metrics_and_fitness(self):
        sr = self.market()
        suppressed = shorts_closed = 0
        for k in range(12):
            g = G.fix(G.random_genome(random.Random(500 + k), G.SPECIES[k % len(G.SPECIES)]))
            g['direction'] = 'both' if k % 2 else 'short'
            new, old, n = self.runs(lambda: X.simulate(g, sr, 60, 1200, curve_every=1))
            self.assertEqual(strip_targets(new), strip_targets(old))
            self.assertEqual(new['metrics'], old['metrics'])
            self.assertEqual(X.fitness(new['metrics']), X.fitness(old['metrics']))
            for p in new['positions']:
                self.assertTrue(p['target'] is None or p['target'] > 0, p)
            suppressed += n
            shorts_closed += sum(1 for t in new['trades'] if t['side'] == -1)
        self.assertGreater(suppressed, 10)            # the scenario really produced negative targets
        self.assertGreater(shorts_closed, 10)         # ... and closed shorts (stop / trailing / reverse)

    def test_simulate_account_identical_including_the_exit_rules_upgrade_and_virtual_positions(self):
        sr = self.market()
        blocked = {'C1-USDT-SWAP': [(100, 500), (800, None)], 'C3-USDT-SWAP': [(50, None)]}
        suppressed = 0
        for k in range(8):
            segs = [dict(id=f's{j}', genes=G.fix(G.random_genome(random.Random(900 + 10 * k + j), G.SPECIES[(k + j) % len(G.SPECIES)])),
                         from_i=0, wind_i=None, boosts=[]) for j in range(3)]
            for s in segs:
                s['genes']['direction'] = 'both'
            # old exits until bar 300 (seat genes stored without a target → no target before, take_profit at the upgrade)
            segs[0]['old_exit'] = dict(target_rr=0, trail=0, max_hold=500, tf=1)
            kw = dict(blocked=blocked, exit_from=300, trail_risk_from=400, off_book_from=59)
            new, old, n = self.runs(lambda: X.simulate_account(segs, sr, 60, 1200, **kw))
            self.assertEqual(strip_targets(new), strip_targets(old))
            for p in new['positions'] + new['off_book']:
                self.assertTrue(p['target'] is None or p['target'] > 0, p)
            suppressed += n
        self.assertGreater(suppressed, 10)

    def test_upgrade_at_exit_rules_from_gives_none_once_and_never_recomputes(self):
        n = 40
        sr = {'A-USDT-SWAP': series_from([100.0] * n, 'A-USDT-SWAP')}
        g = G.random_genome(random.Random(1), 'momentum'); g.update(target_rr=3.0, stop_atr=2.0, risk=.05, leverage=5)
        seg = dict(id='s', genes=g, from_i=0, wind_i=None, boosts=[], old_exit=dict(target_rr=0, trail=0, max_hold=999, tf=1))
        calls = []
        orig = X.take_profit
        with mock.patch.object(X, 'signal_sets', side_effect=lambda gg, series: ({5: [('A-USDT-SWAP', -1, 60.0)]}, {})), \
                mock.patch.object(X, 'take_profit', side_effect=lambda *a: calls.append(a) or orig(*a)):
            r = X.simulate_account([seg], sr, 2, n, exit_from=10)
        p, = r['positions']
        self.assertIsNone(p['target'])                   # 100 − 3 × 60 < 0 → no target after the upgrade
        self.assertEqual(len(calls), 2)                  # the old-era fill (rr 0) and the one upgrade: not once per bar


class ExitsWithoutTargetTests(unittest.TestCase):
    """A short with no take-profit exits by its stop, its trailing stop, or a reverse signal (real and virtual)."""
    def one(self, closes, rev=None, blocked=None, dist=60.0):
        sr = {'A-USDT-SWAP': series_from(closes, 'A-USDT-SWAP', spread=.001), 'B-USDT-SWAP': series_from([100.0] * len(closes), 'B-USDT-SWAP')}
        g = G.random_genome(random.Random(1), 'momentum'); g.update(target_rr=2.0, stop_atr=2.0, risk=.05, leverage=5)
        with mock.patch.object(X, 'signal_sets', side_effect=lambda gg, series: ({5: [('A-USDT-SWAP', -1, dist)]}, rev or {})):
            acc = X.simulate_account([dict(id='s', genes=g, from_i=0, wind_i=None, boosts=[])], sr, 2, len(closes),
                                     blocked=blocked, off_book_from=1 if blocked else None)
            sim = X.simulate(g, {'A-USDT-SWAP': sr['A-USDT-SWAP']}, 2, len(closes), curve_every=1,
                             events={5: [('A-USDT-SWAP', -1, dist)]}, reverse=rev or {})
        return acc, sim

    def test_open_without_target_then_stop(self):
        acc, sim = self.one([100.0] * 10 + [170.0, 170.0])
        for r in (acc, sim):
            t, = r['trades']
            self.assertEqual(t['reason'], 'stop')
            self.assertAlmostEqual(t['entry'], 100.0 * (1 - X.SLIP))

    def test_open_without_target_then_trailing_stop(self):
        # down to 40 (1R = 60 in favour → trail at best + 60 = 100), back up through 100
        acc, sim = self.one([100.0] * 8 + [70.0, 40.0, 40.0, 120.0, 120.0])
        for r in (acc, sim):
            t, = r['trades']
            self.assertEqual(t['reason'], 'trailing_stop')

    def test_open_without_target_then_reverse_signal_and_still_open(self):
        acc, sim = self.one([100.0] * 12, rev={9: {'A-USDT-SWAP': 1}})
        for r in (acc, sim):
            t, = r['trades']
            self.assertEqual((t['reason'], t['closed']), ('reverse_signal', T0 + 9 * H))
        acc, sim = self.one([100.0] * 12)
        for r in (acc, sim):
            p, = r['positions']
            self.assertIsNone(p['target'])

    def test_virtual_position_without_target(self):
        acc, _ = self.one([100.0] * 12, blocked={'A-USDT-SWAP': [(0, None)]})
        v, = acc['off_book']
        self.assertIsNone(v['target']); self.assertEqual(acc['positions'], [])
        acc, _ = self.one([100.0] * 10 + [170.0, 170.0], blocked={'A-USDT-SWAP': [(0, None)]})
        self.assertEqual(acc['off_book_trades'][0]['reason'], 'stop')
        acc, _ = self.one([100.0] * 12, rev={9: {'A-USDT-SWAP': 1}}, blocked={'A-USDT-SWAP': [(0, None)]})
        self.assertEqual(acc['off_book_trades'][0]['reason'], 'reverse_signal')


# ================================================================================================ paper
class PaperSignalTests(unittest.TestCase):
    """signals.json: tp None + no_target True when the target is not valid; a positive tp is published as before.
    Same paper.update harness as test_price_off_live.PaperPublishTests (one momentum seat, target_rr 2.5, flat market)."""
    COINS = POL.PaperPublishTests.COINS
    setUp, sr, state, update = (POL.PaperPublishTests.setUp, POL.PaperPublishTests.sr, POL.PaperPublishTests.state,
                                POL.PaperPublishTests.update)

    def test_tp_fields(self):
        self.assertEqual(P._tp(12.5), {'tp': 12.5})
        for x in (None, 0.0, -0.015):
            self.assertEqual(P._tp(x), {'tp': None, 'no_target': True})

    def test_pending_signal(self):
        sr = {'A-USDT-SWAP': series_from([100.0] * 40, 'A-USDT-SWAP')}
        g = dict(G.random_genome(random.Random(1), 'momentum'), target_rr=2.5)
        with mock.patch.object(P, 'stop_distance', return_value=60.0):
            s = P._signal_from_pending('A-USDT-SWAP', -1, g, sr, T0, 'n1')
            self.assertEqual((s['tp'], s['no_target'], s['stop'], s['dist']), (None, True, 160.0, 60.0))
            s = P._signal_from_pending('A-USDT-SWAP', 1, g, sr, T0, 'n1')
            self.assertEqual(s['tp'], 100.0 + 2.5 * 60.0); self.assertNotIn('no_target', s)
        with mock.patch.object(P, 'stop_distance', return_value=4.0):
            s = P._signal_from_pending('A-USDT-SWAP', -1, g, sr, T0, 'n1')
            self.assertEqual(s['tp'], 100.0 - 2.5 * 4.0); self.assertNotIn('no_target', s)

    def test_positions_pending_and_price_off_live(self):
        # A short (dist 60, 2.5R → −50): an open paper position; B short blocked by price_off: virtual; C long normal
        self.entries = {11: {5: [('A-USDT-SWAP', -1, 60.0), ('B-USDT-SWAP', -1, 60.0), ('C-USDT-SWAP', 1, 4.0)]}}
        v, s = self.update(self.state(), 20, {'B-USDT-SWAP': [[T0, None]]})
        a, c = s['A-USDT-SWAP'], s['C-USDT-SWAP']
        self.assertEqual((a['tp'], a['no_target'], a['side']), (None, True, -1))
        self.assertAlmostEqual(a['stop'], a['entry'] + 60.0)
        self.assertAlmostEqual(c['tp'], c['entry'] + 2.5 * 4.0); self.assertNotIn('no_target', c)
        b = v['price_off_live']['B-USDT-SWAP']
        self.assertEqual((b['tp'], b['no_target'], b['price_off']), (None, True, True))
        json.dumps(s); json.dumps(v['price_off_live'])                                     # still plain JSON (tp null)
        # a queued (pending) entry with a negative target, ordinary and price_off_live
        self.entries = {11: {19: [('A-USDT-SWAP', -1, 60.0), ('B-USDT-SWAP', -1, 60.0)]}}
        with mock.patch.object(P, 'stop_distance', return_value=60.0):
            v, s = self.update(self.state(), 20, {'B-USDT-SWAP': [[T0, None]]})
        self.assertTrue(s['A-USDT-SWAP']['pending']); self.assertEqual((s['A-USDT-SWAP']['tp'], s['A-USDT-SWAP']['no_target']), (None, True))
        b = v['price_off_live']['B-USDT-SWAP']
        self.assertTrue(b['pending']); self.assertEqual((b['tp'], b['no_target']), (None, True))

    def test_signals_feed_the_trader_end_to_end(self):
        """paper → signals.json → fetch_signals.valid → okx_demo_trader: the no-target short opens with the stop only."""
        self.entries = {11: {5: [('A-USDT-SWAP', -1, 60.0)]}}
        _, s = self.update(self.state(), 20, {})
        sig = json.loads(json.dumps(dict(updated_at='x', instruments=['A-USDT-SWAP'], positions=s, exits={})))
        self.assertTrue(fetch_signals.valid(sig))
        saved = (odt.INSTRUMENTS, odt.ACTIVE_PROFILE)
        self.addCleanup(lambda: (setattr(odt, 'INSTRUMENTS', saved[0]), setattr(odt, 'ACTIVE_PROFILE', saved[1])))
        odt.configure_evolved(['A-USDT-SWAP'])
        sp = dict(ctVal='1', lotSz='1', minSz='1', tickSz='0.01', lever='50')
        fake = FakeOKX(prices={'A-USDT-SWAP': 100.0}, instruments={'A-USDT-SWAP': sp})
        p = dict(sig['positions']['A-USDT-SWAP'], opened=NOW_MS - 10 * 60_000)
        state = dict(odt.new_state(), paused=False)
        odt.Mirror(fake, state, {'positions': {'A-USDT-SWAP': p}}, NOW, False).run('run')
        order = next(b for q, b in fake.posts if q == ORDER)
        self.assertEqual(set(order['attachAlgoOrds'][0]), SL_ONLY)


# ================================================================================================ trader
def short_xrp(**kw):
    """A paper short on XRP at 1.25 with a wide stop (dist 0.45): 3R would be 1.25 − 1.35 < 0."""
    return dict(dict(side=-1, entry=1.25, stop=1.70, tp=None, no_target=True, dist=.45, opened=NOW_MS - 30 * 60_000,
                     strategy='evolved', risk=.05, leverage=5, rank=1, champion='c1'), **kw)


class TraderBase(unittest.TestCase):
    GLOBALS = ('INSTRUMENTS', 'RISK', 'SAME_SIDE_CAP', 'ACTIVE_PROFILE', 'DAILY_HALT', 'MARGIN_MODE')

    def setUp(self):
        saved = {k: getattr(odt, k) for k in self.GLOBALS}
        self.addCleanup(lambda: [setattr(odt, k, v) for k, v in saved.items()])
        odt.configure_evolved(['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'XRP-USDT-SWAP'])

    def run_once(self, fake, positions, state=None, live=False, now=NOW, exits=None, pol=None, pol_exits=None):
        state = state if state is not None else dict(odt.new_state(), paused=False)
        paper = {'positions': positions, 'exits': exits or {}}
        if live:
            paper.update(odt.price_off_live_fields(doc(positions, pol, exits, pol_exits)))
        m = odt.Mirror(fake, state, paper, now, False)
        m.follow_price_off = live
        m.run('run')
        return m, state

    def order(self, fake):
        return next((b for q, b in fake.posts if q == ORDER), None)

    def assert_sl_only(self, fake, stop):
        o = self.order(fake)
        self.assertIsNotNone(o, fake.posts)
        algo, = o['attachAlgoOrds']
        self.assertEqual(set(algo), SL_ONLY)
        self.assertEqual(algo['slTriggerPx'], stop)
        self.assertFalse(any('tpTriggerPx' in (b or {}) or 'newTpTriggerPx' in (b or {}) for _, b in fake.posts))
        return o


class OrdinaryOpenTests(TraderBase):
    def check_no_target_open(self, sig, live, stop='1.7'):
        fake = FakeOKX()
        m, state = self.run_once(fake, {'XRP-USDT-SWAP': sig}, live=live)
        self.assert_sl_only(fake, stop)
        rec = state['mirrored'][f"XRP-USDT-SWAP|{sig['opened']}"]
        self.assertEqual((rec['status'], rec['target'], rec['no_target'], rec['stop']), ('open', None, True, float(stop)))
        a = next(a for a in m.actions if a['action'] == 'open')
        self.assertEqual((a['target'], a['no_target']), (None, True))
        return fake, state, rec

    def test_no_target_signal_opens_with_the_stop_only_live_and_demo(self):
        for live in (False, True):
            with self.subTest(live=live):
                self.check_no_target_open(short_xrp(), live)

    def test_no_target_never_falls_back_to_3r_even_when_3r_is_valid(self):
        sig = short_xrp(stop=1.40, dist=.15)                       # 3R = 0.80 > 0, but the paper said no target
        self.check_no_target_open(sig, False, stop='1.4')

    def test_old_signals_with_a_negative_tp_float_open_without_tp(self):
        for tp in (-0.015, 0.0, -1e9):
            with self.subTest(tp=tp):
                sig = short_xrp(tp=tp); sig.pop('no_target')
                self.check_no_target_open(sig, live=True)

    def test_legacy_tp_none_keeps_3r_when_positive_and_drops_it_when_not(self):
        sig = short_xrp(stop=1.40, dist=None); sig.pop('no_target'); sig.pop('dist')     # signals from before 2026-11-21
        fake = FakeOKX(); _, state = self.run_once(fake, {'XRP-USDT-SWAP': sig})
        algo = self.order(fake)['attachAlgoOrds'][0]
        self.assertEqual(algo['tpTriggerPx'], '0.8')                  # 1.25 − 3 × 0.15
        rec = next(iter(state['mirrored'].values()))
        self.assertAlmostEqual(rec['target'], 1.25 - 3 * .15); self.assertNotIn('no_target', rec)
        sig = short_xrp(); sig.pop('no_target'); sig.pop('dist')    # 3R = 1.25 − 1.35 < 0
        self.check_no_target_open(sig, live=False)

    def test_wrong_side_or_tick_rounded_to_zero_targets_open_without_tp(self):
        self.check_no_target_open(dict(short_xrp(tp=1.30), no_target=False), live=False)   # above the entry of a short
        self.check_no_target_open(dict(short_xrp(tp=0.00004), no_target=False), live=False)  # 0.0000 on tick 0.0001

    def test_positive_tp_is_unchanged(self):
        fake = FakeOKX(); _, state = self.run_once(fake, {'XRP-USDT-SWAP': short_xrp(tp=1.10, no_target=False)})
        self.assertEqual(self.order(fake)['attachAlgoOrds'][0]['tpTriggerPx'], '1.1')
        self.assertNotIn('no_target', next(iter(state['mirrored'].values())))
        # a valid tp the live price has already passed is still skipped
        fake = FakeOKX(prices={'XRP-USDT-SWAP': 1.05, 'BTC-USDT-SWAP': 70000.0})
        m, _ = self.run_once(fake, {'XRP-USDT-SWAP': short_xrp(tp=1.10, no_target=False)})
        self.assertEqual(fake.posts, []); self.assertEqual(m.actions[-1]['reason'], 'target_already_reached')

    def test_risk_size_and_leverage_are_unchanged_by_the_missing_target(self):
        a, b = FakeOKX(), FakeOKX()
        self.run_once(a, {'XRP-USDT-SWAP': short_xrp()})
        self.run_once(b, {'XRP-USDT-SWAP': short_xrp(tp=0.5, no_target=False)})
        oa, ob = self.order(a), self.order(b)
        self.assertEqual({k: v for k, v in oa.items() if k != 'attachAlgoOrds'}, {k: v for k, v in ob.items() if k != 'attachAlgoOrds'})
        self.assertEqual(a.posts[0], b.posts[0])                    # set-leverage
        self.assertEqual(oa['attachAlgoOrds'][0]['slTriggerPx'], ob['attachAlgoOrds'][0]['slTriggerPx'])

    def test_manual_eth_short_still_skipped(self):
        eth = dict(instId='ETH-USDT-SWAP', posSide='net', pos='-7.41', avgPx='1687.04', markPx='1651.35', upl='26.4', lever='100', mgnMode='cross')
        fake = FakeOKX(positions=[eth], pos_mode='net_mode', prices={'ETH-USDT-SWAP': 1651.35, 'XRP-USDT-SWAP': 1.25, 'BTC-USDT-SWAP': 70000.0})
        sig = dict(short_xrp(), entry=1651.35, stop=2500.0, dist=848.65)     # 3R and 6R both < 0
        for live in (False, True):
            m, state = self.run_once(fake, {'ETH-USDT-SWAP': sig}, live=live)
            self.assertEqual(fake.posts, [])
            self.assertEqual([a['reason'] for a in m.actions if a['inst'] == 'ETH-USDT-SWAP'], ['exchange_position_exists_not_ours'])


class ManageNoTargetTests(TraderBase):
    """Later runs: no TP re-added / amended / closed for, trailing still works, exchange close = stop / trailing stop."""
    def opened(self, sig=None, live=False):
        sig = sig or short_xrp()
        fake = FakeOKX()
        _, state = self.run_once(fake, {'XRP-USDT-SWAP': sig}, live=live)
        key = f"XRP-USDT-SWAP|{sig['opened']}"
        sz = self.order(fake)['sz']
        fake.positions = [dict(instId='XRP-USDT-SWAP', posSide='short', pos=sz, mgnMode='cross', avgPx='1.25')]
        fake.algos = [dict(instId='XRP-USDT-SWAP', posSide='short', algoId='s1', slTriggerPx='1.7')]   # stop only, as placed
        fake.posts.clear()
        return fake, state, key, sig

    def test_ensure_target_does_nothing(self):
        for sig in (short_xrp(), dict(short_xrp(tp=-0.015), no_target=False)):
            fake, state, key, sig = self.opened(sig)
            for h in (1, 2, 30):
                m, _ = self.run_once(fake, {'XRP-USDT-SWAP': sig}, state=state, now=NOW + timedelta(hours=h))
                self.assertEqual(fake.posts, [])
                self.assertFalse([a for a in m.actions if a['action'] in ('error', 'restore_target', 'amend_target', 'close')], m.actions)
            # even with the price "past" the old negative target or 3R, never closed as take_profit
            fake.prices = dict(fake.prices, **{'XRP-USDT-SWAP': 0.0001})
            m, _ = self.run_once(fake, {'XRP-USDT-SWAP': sig}, state=state, now=NOW + timedelta(hours=31))
            self.assertFalse(any(a.get('reason') == 'take_profit' for a in m.actions))
            self.assertEqual(state['mirrored'][key]['status'], 'open')

    def test_ensure_target_never_places_or_amends_a_negative_tp_on_an_older_mirror(self):
        """A mirror without no_target (e.g. opened by an older trader) and a signal whose tp is negative / its 3R is ≤ 0."""
        fake, state, key, sig = self.opened()
        m0 = state['mirrored'][key]
        m0.pop('no_target'); m0.pop('target')
        old = dict(sig, tp=-0.015); old.pop('no_target')
        m, _ = self.run_once(fake, {'XRP-USDT-SWAP': old}, state=state, now=NOW + timedelta(hours=1))
        self.assertEqual(fake.posts, []); self.assertFalse([a for a in m.actions if a['action'] == 'error'])
        self.assertEqual((m0['no_target'], m0['target']), (True, None))
        # with a positive stored target resting on the exchange, a negative paper tp never amends it
        fake, state, key, sig = self.opened(short_xrp(tp=1.0, no_target=False))
        fake.algos[0]['tpTriggerPx'] = '1'
        old = dict(sig, tp=-0.015)
        self.run_once(fake, {'XRP-USDT-SWAP': old}, state=state, now=NOW + timedelta(hours=1))
        self.assertEqual(fake.posts, [])
        # ... and when that TP is missing it is restored at the positive stored target, not the negative paper one
        fake.algos = [dict(instId='XRP-USDT-SWAP', posSide='short', algoId='s1', slTriggerPx='1.7')]
        self.run_once(fake, {'XRP-USDT-SWAP': old}, state=state, now=NOW + timedelta(hours=2))
        self.assertEqual([b['tpTriggerPx'] for _, b in fake.posts], ['1'])

    def test_trailing_stop_still_trails_and_closes(self):
        fake, state, key, sig = self.opened()
        bar = lambda h, hi, lo: [str(NOW_MS + h * H), '1.2', str(hi), str(lo), '1.0', '1', '1', '1', '1']
        fake.candles = {'XRP-USDT-SWAP': [bar(1, 1.25, 0.90)]}       # best 0.90: 0.35 ≥ 0.5 × 0.45 → stop 0.90 + 0.45 = 1.35
        fake.prices = dict(fake.prices, **{'XRP-USDT-SWAP': 1.0})
        self.run_once(fake, {'XRP-USDT-SWAP': sig}, state=state, now=NOW + timedelta(hours=2))
        self.assertEqual(fake.posts, [('/api/v5/trade/amend-algos', {'instId': 'XRP-USDT-SWAP', 'algoId': 's1', 'newSlTriggerPx': '1.35'})])
        mir = state['mirrored'][key]
        self.assertTrue(mir['trailing']); self.assertEqual(mir['stop'], 1.35)
        # price already above the next trailed stop → closed at market as trailing_stop
        fake.posts.clear(); fake.algos[0]['slTriggerPx'] = '1.35'
        fake.candles = {'XRP-USDT-SWAP': [bar(3, 1.0, 0.70), bar(1, 1.25, 0.90)]}    # best 0.70 → stop 1.15
        fake.prices = dict(fake.prices, **{'XRP-USDT-SWAP': 1.2})
        m, _ = self.run_once(fake, {'XRP-USDT-SWAP': sig}, state=state, now=NOW + timedelta(hours=4))
        self.assertEqual(fake.posts[0][0], '/api/v5/trade/close-position')
        self.assertEqual(mir['closed_reason'], 'trailing_stop')

    def test_exchange_close_in_profit_is_a_stop_never_a_take_profit(self):
        for trailing, want in ((False, 'stop_loss'), (True, 'trailing_stop')):
            fake, state, key, sig = self.opened()
            mir = state['mirrored'][key]
            if trailing:
                mir.update(stop=1.15, trailing=True)
            fake.positions = []
            fake.history = [dict(instId='XRP-USDT-SWAP', posSide='short', realizedPnl='120.5', closeAvgPx='1.15', type='2', uTime=str(NOW_MS + 60_000))]
            m, _ = self.run_once(fake, {'XRP-USDT-SWAP': sig}, state=state, now=NOW + timedelta(hours=3))
            a = next(a for a in m.actions if a['action'] == 'closed_on_exchange')
            self.assertEqual((mir['closed_reason'], a['reason'], a['outcome'], a['pnl']), (want, want, 'stop', 120.5))
        # outcome() itself: no_target or a None target is always the stop; a liquidation stays a liquidation
        m = odt.Mirror(FakeOKX(), dict(odt.new_state()), {}, NOW, False)
        for rec in (dict(stop=1.0, target=None, no_target=True), dict(stop=1.0, target=None), dict(stop=1.0)):
            self.assertEqual(m.outcome(rec, dict(pnl=50.0, close_price=0.5))['outcome'], 'stop')
        self.assertEqual(m.outcome(dict(stop=1.0, target=None), dict(pnl=-9.0, close_price=2.0, liquidated=True))['outcome'], 'liquidation')
        self.assertEqual(m.outcome(dict(stop=1.0, target=0.5), dict(pnl=50.0, close_price=0.51))['outcome'], 'target')   # unchanged

    def test_paper_close_and_reverse_signal_close_cleanly(self):
        for why, want in (('reverse_signal', 'reverse_signal'), ('trailing_stop', 'trailing_stop'), ('stop', 'paper_closed'), (None, 'paper_closed')):
            fake, state, key, sig = self.opened()
            m, _ = self.run_once(fake, {}, state=state, now=NOW + timedelta(hours=1), exits={key: why} if why else {})
            self.assertEqual(fake.posts[0][0], '/api/v5/trade/close-position')
            self.assertEqual(state['mirrored'][key]['closed_reason'], want)
            self.assertFalse([a for a in m.actions if a['action'] == 'error'])


class PriceOffLiveNoTargetTests(TraderBase):
    def setUp(self):
        super().setUp()
        self.opened = NOW_MS - 50 * 60_000

    def fake(self, **kw):
        f = LiveFakeOKX(**kw)
        f.candles = {'XRP-USDT-SWAP': candles(60, self.opened - H, base=1.25, step=.01),
                     'ETH-USDT-SWAP': candles(60, self.opened - H, base=1650.0, step=8.0)}
        return f

    def live(self, fake, pol, state=None, now=NOW, pol_exits=None):
        return self.run_once(fake, {}, state=state, live=True, now=now, pol=pol, pol_exits=pol_exits)

    def test_live_negative_tp_opens_with_the_stop_only(self):
        fake = self.fake()
        sig = pol_signal(side=-1, opened=self.opened, entry=1.25, stop=1.40, tp=0.5, stop_atr=30.0, target_rr=6.0)
        m, state = self.live(fake, {'XRP-USDT-SWAP': sig})
        dist = 30.0 * real_atr(fake.candles['XRP-USDT-SWAP'])
        self.assertLess(1.25 - 6.0 * dist, 0)
        self.assert_sl_only(fake, odt.fmt(odt.quantize(1.25 + dist, '0.0001', odt.ROUND_HALF_UP)))
        rec = state['mirrored'][f'XRP-USDT-SWAP|{self.opened}']
        self.assertEqual((rec['target'], rec['no_target'], rec['sim_tp'], rec['price_off_live']), (None, True, 0.5, True))
        a = next(a for a in m.actions if a['action'] == 'open')
        self.assertEqual((a['target'], a['no_target'], a['sim_tp']), (None, True, 0.5))
        # the size is the normal rule at the live distance (XRP major: 5 % × 2 = 10 % of 5000)
        self.assertEqual(self.order(fake)['sz'], odt.fmt(odt.quantize(500 / dist / 100, '1')))

    def test_stop_not_positive_is_still_skipped(self):
        fake = self.fake()
        sig = pol_signal(side=1, opened=self.opened, stop_atr=30.0, target_rr=2.0)      # long, 30 × ATR > price → stop < 0
        m, state = self.live(fake, {'XRP-USDT-SWAP': sig})
        self.assertEqual(fake.posts, [])
        self.assertEqual(m.actions[-1]['reason'], 'stop_not_positive')
        self.assertLess(m.actions[-1]['stop'], 0)

    def test_live_decision_replaces_the_sim_one(self):
        # sim said no_target, live tp is valid → TP placed at the live level, record without no_target
        fake = self.fake()
        sig = pol_signal(opened=self.opened, tp=None, no_target=True)
        _, state = self.live(fake, {'XRP-USDT-SWAP': sig})
        dist = 2.0 * real_atr(fake.candles['XRP-USDT-SWAP'])
        self.assertEqual(self.order(fake)['attachAlgoOrds'][0]['tpTriggerPx'], odt.fmt(odt.quantize(1.25 + 2.0 * dist, '0.0001', odt.ROUND_HALF_UP)))
        rec = state['mirrored'][f'XRP-USDT-SWAP|{self.opened}']
        self.assertNotIn('no_target', rec); self.assertAlmostEqual(rec['target'], 1.25 + 2 * dist)
        # later run: the sim's no_target does not stop ensure_target from restoring live's own TP
        fake.positions = [dict(instId='XRP-USDT-SWAP', posSide='long', pos=self.order(fake)['sz'], avgPx='1.25')]
        fake.algos = [dict(instId='XRP-USDT-SWAP', posSide='long', algoId='a1', slTriggerPx=str(rec['stop']))]
        fake.posts.clear()
        self.live(fake, {'XRP-USDT-SWAP': sig}, state=state, now=NOW + timedelta(hours=1))
        self.assertEqual([b.get('tpTriggerPx') for q, b in fake.posts if q == '/api/v5/trade/order-algo'],
                         [odt.fmt(odt.quantize(rec['target'], '0.0001', odt.ROUND_HALF_UP))])
        # sim had a positive tp, live's is ≤ 0 → live opens no_target and never adds the sim tp later
        fake = self.fake()
        sig = pol_signal(side=-1, opened=self.opened, entry=1.25, stop=1.40, tp=1.10, stop_atr=30.0, target_rr=6.0)
        _, state = self.live(fake, {'XRP-USDT-SWAP': sig})
        self.assert_sl_only(fake, self.order(fake)['attachAlgoOrds'][0]['slTriggerPx'])
        rec = state['mirrored'][f'XRP-USDT-SWAP|{self.opened}']
        fake.positions = [dict(instId='XRP-USDT-SWAP', posSide='short', pos=self.order(fake)['sz'], avgPx='1.25')]
        fake.algos = [dict(instId='XRP-USDT-SWAP', posSide='short', algoId='a1', slTriggerPx=str(rec['stop']))]
        fake.posts.clear()
        m, _ = self.live(fake, {'XRP-USDT-SWAP': sig}, state=state, now=NOW + timedelta(hours=1))
        self.assertEqual([q for q, _ in fake.posts if q != '/api/v5/trade/amend-algos'], [])
        self.assertFalse(any('tpTriggerPx' in b or 'newTpTriggerPx' in b for _, b in fake.posts))
        self.assertFalse([a for a in m.actions if a['action'] == 'error'])

    def test_price_off_live_exit_closes_a_no_target_position(self):
        fake = self.fake()
        sig = pol_signal(side=-1, opened=self.opened, entry=1.25, stop=1.40, tp=None, no_target=True, stop_atr=30.0, target_rr=6.0)
        _, state = self.live(fake, {'XRP-USDT-SWAP': sig})
        key = f'XRP-USDT-SWAP|{self.opened}'
        fake.positions = [dict(instId='XRP-USDT-SWAP', posSide='short', pos=self.order(fake)['sz'], avgPx='1.25')]
        fake.algos = [dict(instId='XRP-USDT-SWAP', posSide='short', algoId='a1', slTriggerPx=str(state['mirrored'][key]['stop']))]
        fake.posts.clear()
        self.live(fake, {}, state=state, now=NOW + timedelta(hours=1), pol_exits={key: 'reverse_signal'})
        self.assertEqual(fake.posts[0][0], '/api/v5/trade/close-position')
        self.assertEqual(state['mirrored'][key]['closed_reason'], 'reverse_signal')

    def test_manual_eth_short_still_skipped(self):
        eth = dict(instId='ETH-USDT-SWAP', posSide='net', pos='-7.41', avgPx='1687.04', markPx='1651.35', upl='26.4', lever='100', mgnMode='cross')
        fake = self.fake(positions=[eth], pos_mode='net_mode', prices={'ETH-USDT-SWAP': 1651.35, 'XRP-USDT-SWAP': 1.25, 'BTC-USDT-SWAP': 70000.0})
        sig = pol_signal('ETH-USDT-SWAP', side=-1, opened=self.opened, entry=1820.0, stop=1900.0, tp=None, no_target=True, stop_atr=300.0, target_rr=6.0)
        m, _ = self.live(fake, {'ETH-USDT-SWAP': sig})
        self.assertEqual(fake.posts, []); self.assertEqual(fake.candle_paths, [])
        self.assertEqual([a['reason'] for a in m.actions if a['inst'] == 'ETH-USDT-SWAP'], ['exchange_position_exists_not_ours'])


class CompatTests(unittest.TestCase):
    def test_fetch_signals_accepts_the_new_fields(self):
        p = dict(side=-1, entry=0.0264, stop=0.0347, tp=None, no_target=True, opened=1, dist=.0083)
        sig = dict(updated_at='2026-11-25T10:00:00Z', instruments=['H-USDT-SWAP'], positions={'H-USDT-SWAP': p},
                   price_off_live={'H-USDT-SWAP': dict(p, tf=24, stop_atr=2.0, price_off=True)})
        self.assertTrue(fetch_signals.valid(json.loads(json.dumps(sig))))
        f = odt.price_off_live_fields(sig)
        self.assertIn('H-USDT-SWAP', f['price_off_live']); self.assertEqual(f['price_off_live_invalid'], set())

    def test_valid_tp_and_target_of(self):
        for x in (None, '', 'x', 0, -1.0, float('nan'), float('inf')):
            self.assertIsNone(odt.valid_tp(x), x)
        self.assertEqual(odt.valid_tp('1.5'), 1.5)
        self.assertEqual(odt.target_of(dict(side=-1, no_target=True, tp=0.9), 1.0), (None, None))
        self.assertEqual(odt.target_of(dict(side=-1, tp=0.9, entry=1.0), 1.0), (0.9, None))
        self.assertEqual(odt.target_of(dict(side=-1, tp=0.9, entry=1.0), 0.85), (0.9, 'target_already_reached'))
        self.assertEqual(odt.target_of(dict(side=-1, tp=-0.1, entry=1.0), 1.0), (None, None))
        self.assertEqual(odt.target_of(dict(side=1, tp=0.9, entry=1.0), 1.0), (None, None))           # below a long's entry
        self.assertAlmostEqual(odt.target_of(dict(side=1, tp=None, entry=1.0, stop=0.9), 1.0)[0], 1.0 + 3 * 0.1)
        self.assertEqual(odt.target_of(dict(side=-1, tp=None, entry=1.0, stop=1.5), 1.0), (None, None))
        # price_off_live: the live tp is checked against the live price, not the sim entry
        self.assertEqual(odt.target_of(dict(side=-1, tp=1.19, entry=1.15, price_off_live=True), 1.25), (1.19, None))


class LiveSyncNoTargetTests(unittest.TestCase):
    """live-data: a no-target mirror is published without a target (no null, no 0) and its exchange close as a stop."""
    def test_snapshot_and_trades(self):
        from test_live_sync import sample_summary
        mir = {'inst': 'H-USDT-SWAP', 'side': -1, 'posSide': 'net', 'strategy': 'evolved', 'contracts': '14.6', 'stop': 0.03466,
               'target': None, 'no_target': True, 'leverage': 7, 'entry': 0.02641, 'opened_at': 1794751218000, 'status': 'open',
               'champion': 's23387', 'mgnMode': 'cross'}
        summary = sample_summary(positions=[{'inst': 'H-USDT-SWAP', 'side': 'short', 'contracts': '14.6', 'avgPx': '0.02641', 'markPx': '0.026',
                                             'upl': '0.06', 'lever': '7', 'mgnMode': 'cross', 'ctVal': 100.0, 'ours': True}],
                                 open_mirrors=[], recent_actions=[])
        snap = ls.build_snapshot(summary, {'mode': 'live', 'mirrored': {'H-USDT-SWAP|1': mir}})
        pos, = snap['positions']
        self.assertEqual(pos['stop'], 0.03466); self.assertNotIn('target', pos)
        actions = [
            {'ts': '2026-11-15T14:00:18Z', 'action': 'open', 'inst': 'H-USDT-SWAP', 'side': -1, 'strategy': 'evolved', 'contracts': '14.6',
             'price': 0.02641, 'stop': '0.03466', 'target': None, 'no_target': True, 'leverage': 7, 'risk_usdt': 12.12, 'dry_run': False},
            {'ts': '2026-11-15T20:00:00Z', 'action': 'closed_on_exchange', 'inst': 'H-USDT-SWAP', 'strategy': 'evolved', 'pnl': 5.1,
             'close_price': 0.0230, 'outcome': 'stop', 'reason': 'trailing_stop', 'dry_run': False},
        ]
        done = dict(mir, status='done', closed_reason='trailing_stop', trailing=True, stop=0.0230)
        fills = ls.build_fills(actions, summary)
        json.dumps(fills)
        trades = ls.build_trades(actions, {'mode': 'live', 'mirrored': {'H-USDT-SWAP|1': done}}, summary)
        t, = trades['trades']
        self.assertNotIn('target', t)
        self.assertEqual((t['exit_type'], t['outcome'], t['reason'], t['pnl']), ('sl', 'stop', 'trailing_stop', 5.1))
        # without a recorded outcome the stop / target proximity rule needs a target: none → 'unknown', never 'tp'
        a2 = [actions[0], dict(actions[1], outcome=None, pnl=None)]
        t, = ls.build_trades(a2, {'mode': 'live', 'mirrored': {'H-USDT-SWAP|1': done}}, summary)['trades']
        self.assertNotEqual(t['exit_type'], 'tp')


class MainNoTargetTests(LiveModeBase):
    """main() with a fake curl: live and demo send the same SL-only order for a no-target signal."""
    def test_live_and_demo_place_the_same_sl_only_order(self):
        now = datetime.now(timezone.utc)
        sig = dict(updated_at=now.isoformat(), champion='c1', team=['c1'], instruments=['BTC-USDT-SWAP'],
                   positions={'BTC-USDT-SWAP': dict(side=-1, entry=70000.0, stop=95000.0, tp=None, no_target=True, dist=25000.0,
                                                    opened=int(now.timestamp() * 1000) - 20 * 60_000, strategy='evolved', leverage=7,
                                                    risk=.05, rank=1, champion='c1')})
        self.write_signals(sig)
        bodies = {}
        for mode in ('demo', 'live'):
            env = dict(OKX_TRADING_MODE=mode) if mode == 'live' else {}
            self.main(**env, OKX_DRY_RUN='0'); self.requests()
            self.resume(mode)
            text, code = self.main(**env, OKX_DRY_RUN='0')
            self.assertIsNone(code, text)
            bodies[mode] = [(r['path'], json.loads(r['body'])) for r in self.requests() if r['method'] == 'POST']
            state = json.loads((self.data / odt.STATE_DIRS[mode] / 'state.json').read_text())
            rec, = state['mirrored'].values()
            self.assertEqual((rec['target'], rec['no_target']), (None, True))
        self.assertEqual(bodies['demo'], bodies['live'])
        (_, lev), (path, order) = bodies['live']
        self.assertEqual(path, ORDER)
        self.assertEqual(order['attachAlgoOrds'], [dict(slTriggerPx='95000', slOrdPx='-1', slTriggerPxType='last')])
        self.assertEqual(order['sz'], '4')            # 10 % of 10 000 at a 25 000 stop distance = 0.04 BTC = 4 contracts


if __name__ == '__main__':
    unittest.main()
