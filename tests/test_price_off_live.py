"""PRICE_OFF LIVE (2026-11-25): price_off (OKX demo price vs real market) keeps gating the paper account and the demo
follower, but no longer drops the signals the LIVE follower trades on.

Paper side: execution.simulate_account(off_book_from=…) tracks the entries refused only by price_off as virtual
positions that never touch the account; paper.update publishes them (and the queued ones) as price_off_live, with their
own exits. Live side: okx_demo_trader follows them in live mode only, recomputes stop / take-profit from real candles,
and keeps every other gate. Nothing here reaches OKX: fake clients / a fake curl only.
"""
import io
import json
import math
import os
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'evolve'))
sys.path.insert(0, str(ROOT / 'tools'))
import execution as X      # noqa: E402
import genome as G         # noqa: E402
import paper as P          # noqa: E402
import okx_demo_trader as odt  # noqa: E402
sys.path.insert(0, str(ROOT / 'tests'))
from test_okx_demo_trader import FakeOKX, NOW, NOW_MS  # noqa: E402
from test_okx_live_mode import LiveModeBase, FAKE_CURL, KEY  # noqa: E402

H = 3600_000
T0 = 1_760_000_000_000 // H * H


# ------------------------------------------------------------------------------------------------------------ paper
def walk(n, seed):
    rng, x, out = random.Random(seed), 100.0, []
    for _ in range(n):
        x *= math.exp(rng.gauss(0, .012)); out.append(x)
    return out


def series_from(closes, inst, spread=.004):
    o = [closes[0]] + closes[:-1]
    h = [max(a, b) * (1 + spread) for a, b in zip(o, closes)]
    l = [min(a, b) * (1 - spread) for a, b in zip(o, closes)]
    return X.Series(inst, [T0 + i * H for i in range(len(closes))], list(o), h, l, list(closes))


class SimulateAccountOffBookTests(unittest.TestCase):
    def market(self, n=900, coins=6):
        return {f'C{k}-USDT-SWAP': series_from(walk(n, 10 + k), f'C{k}-USDT-SWAP') for k in range(coins)}

    def test_paper_account_is_identical_with_and_without_tracking(self):
        sr = self.market()
        blocked = {'C1-USDT-SWAP': [(100, 400), (600, None)], 'C3-USDT-SWAP': [(50, None)]}
        tracked = 0
        for k in range(10):
            segs = [dict(id=f's{j}', genes=G.random_genome(random.Random(100 * k + j), G.SPECIES[(k + j) % len(G.SPECIES)]), from_i=0,
                         wind_i=None, boosts=[]) for j in range(3)]
            a = X.simulate_account(segs, sr, 60, 900, blocked=blocked)
            b = X.simulate_account(segs, sr, 60, 900, blocked=blocked, off_book_from=59)
            self.assertEqual(set(b) - set(a), {'off_book', 'off_book_trades'})
            self.assertEqual({k2: v for k2, v in b.items() if k2 not in ('off_book', 'off_book_trades')}, a)   # byte-identical account
            for t in b['off_book_trades'] + b['off_book']:
                bar = (t['opened'] - T0) // H
                self.assertTrue(any(x <= bar and (y is None or bar < y) for x, y in blocked[t['inst']]), t)  # only price_off-blocked fills
            tracked += len(b['off_book_trades']) + len(b['off_book'])
        self.assertGreater(tracked, 0)                                       # the test exercised virtual positions
        # nothing blocked → nothing virtual
        segs = [dict(id='s', genes=G.random_genome(random.Random(3), 'momentum'), from_i=0, wind_i=None, boosts=[])]
        r = X.simulate_account(segs, sr, 60, 900, off_book_from=59)
        self.assertEqual((r['off_book'], r['off_book_trades']), ([], []))

    def _one(self, closes, blocked_from, entries, rev=None, rr=2.0):
        sr = {'A-USDT-SWAP': series_from(closes, 'A-USDT-SWAP'), 'B-USDT-SWAP': series_from([100.0] * len(closes), 'B-USDT-SWAP')}
        g = G.random_genome(random.Random(1), 'momentum'); g.update(target_rr=rr, stop_atr=2.0, risk=.05, leverage=5)
        with mock.patch.object(X, 'signal_sets', side_effect=lambda gg, series: (entries, rev or {})):
            return X.simulate_account([dict(id='s', genes=g, from_i=0, wind_i=None, boosts=[])], sr, 2, len(closes),
                                      blocked={'A-USDT-SWAP': [(blocked_from, None)]}, off_book_from=1)

    def test_virtual_position_levels_and_stop_exit(self):
        closes = [100.0] * 12 + [97.0, 90.0, 90.0]
        r = self._one(closes, 3, {5: [('A-USDT-SWAP', 1, 4.0)]})
        self.assertEqual(r['positions'], []); self.assertEqual(r['trades'], [])          # the paper account never had it
        t, = r['off_book_trades']
        entry = 100.0 * (1 + X.SLIP)                                                       # fill at the open of bar 6, with slippage
        self.assertAlmostEqual(t['entry'], entry); self.assertEqual(t['opened'], T0 + 6 * H)
        self.assertEqual(t['reason'], 'stop'); self.assertAlmostEqual(t['exit'], entry - 4.0)   # bar 13 (97 → 90) crosses stop 96.02
        self.assertEqual(t['closed'], T0 + 13 * H)

    def test_virtual_target_and_reverse_signal_exits(self):
        r = self._one([100.0] * 8 + [104.0, 110.0, 110.0], 3, {5: [('A-USDT-SWAP', 1, 4.0)]}, rr=2.0)
        self.assertEqual(r['off_book_trades'][0]['reason'], 'target')
        r = self._one([100.0] * 12, 3, {5: [('A-USDT-SWAP', 1, 4.0)]}, rev={8: {'A-USDT-SWAP': -1}})
        t, = r['off_book_trades']
        self.assertEqual((t['reason'], t['closed']), ('reverse_signal', T0 + 8 * H))
        r = self._one([100.0] * 12, 3, {5: [('A-USDT-SWAP', 1, 4.0)]})
        p, = r['off_book']                                                                  # still open: published with its levels
        self.assertAlmostEqual(p['target'], p['entry'] + 2.0 * 4.0)

    def test_unblocked_signal_is_a_real_position_not_a_virtual_one(self):
        r = self._one([100.0] * 12, 50, {5: [('A-USDT-SWAP', 1, 4.0)]})                      # block starts after the fill
        self.assertEqual([p['inst'] for p in r['positions']], ['A-USDT-SWAP'])
        self.assertEqual(r['off_book'], [])


    # ---- PRICE_OFF LIVE same-bar share (Codex P1): paper + price_off entries of one seat / bar / side share ONE risk on live
    def _mixed(self, entries, blocked, n=14, off_book_from=1, held_first=None):
        coins = ('A-USDT-SWAP', 'B-USDT-SWAP', 'C-USDT-SWAP', 'D-USDT-SWAP')
        sr = {k: series_from([100.0] * n, k) for k in coins}
        g = G.random_genome(random.Random(1), 'momentum'); g.update(target_rr=2.0, stop_atr=2.0, risk=.05, leverage=5)
        seg = dict(id='s', genes=g, from_i=0, wind_i=None, boosts=[], tenure_ts=T0 - 100 * H)    # seated long ago: no new-seat half
        with mock.patch.object(X, 'signal_sets', side_effect=lambda gg, series: (entries, {})):
            return X.simulate_account([seg], sr, 2, n, blocked=blocked, off_book_from=off_book_from)

    def test_mixed_same_bar_entries_share_one_risk_on_live_and_paper_is_unchanged(self):
        for order in ((('A-USDT-SWAP', 1, 4.0), ('B-USDT-SWAP', 1, 4.0)), (('B-USDT-SWAP', 1, 4.0), ('A-USDT-SWAP', 1, 4.0))):
            entries, blocked = {5: list(order)}, {'A-USDT-SWAP': [(3, None)]}
            on = self._mixed(entries, blocked)
            off = self._mixed(entries, blocked, off_book_from=None)
            b, = on['positions']; v, = on['off_book']
            self.assertEqual((b['inst'], v['inst']), ('B-USDT-SWAP', 'A-USDT-SWAP'))
            self.assertEqual(b['risk_factor'], 1.0)                      # the paper account takes one entry: full risk, unchanged
            self.assertEqual(b['live_risk_factor'], .5)                  # live opens both: ½ + ½ = one position's risk
            self.assertEqual(v['risk_factor'], .5)
            self.assertEqual(b['live_risk_factor'] + v['risk_factor'], 1.0)
            pb, = off['positions']                                       # paper sizing byte-identical without tracking
            self.assertEqual({k: x for k, x in b.items() if k != 'live_risk_factor'}, pb)
            self.assertNotIn('live_risk_factor', pb)
            self.assertEqual((on['equity'], on['cash'], on['curve'], on['trades']), (off['equity'], off['cash'], off['curve'], off['trades']))
            self.assertAlmostEqual(b['risk'], .05); self.assertEqual(b['qty'], pb['qty'])

    def test_live_group_counts_paper_and_virtual_slots_in_fill_order(self):
        # the seat already holds 2 (C, D at bar 3); bar 5: A (price_off) and B (paper) compete for the last slot
        blocked = {'A-USDT-SWAP': [(4, None)]}
        held = [('C-USDT-SWAP', 1, 4.0), ('D-USDT-SWAP', 1, 4.0)]
        a_first = self._mixed({3: held, 5: [('A-USDT-SWAP', 1, 4.0), ('B-USDT-SWAP', 1, 4.0)]}, blocked)
        b = next(p for p in a_first['positions'] if p['inst'] == 'B-USDT-SWAP'); v, = a_first['off_book']
        # A takes the virtual slot first (2 + 0 < 3), then B the paper one (2 < 3; the paper ignores the virtual book): both
        # open on live, so they share: ½ + ½
        self.assertEqual((b['risk_factor'], b['live_risk_factor'], v['risk_factor']), (1.0, .5, .5))
        b_first = self._mixed({3: held, 5: [('B-USDT-SWAP', 1, 4.0), ('A-USDT-SWAP', 1, 4.0)]}, blocked)
        b = next(p for p in b_first['positions'] if p['inst'] == 'B-USDT-SWAP')
        self.assertEqual(b_first['off_book'], [])                       # B filled the seat: no virtual A, so B keeps its full risk
        self.assertEqual(b['risk_factor'], 1.0); self.assertNotIn('live_risk_factor', b)
        # a coin already in the virtual book is not counted again (the live follower holds one position per coin)
        r = self._mixed({5: [('A-USDT-SWAP', 1, 4.0)], 8: [('A-USDT-SWAP', 1, 4.0), ('B-USDT-SWAP', 1, 4.0)]}, {'A-USDT-SWAP': [(3, None)]})
        b, = r['positions']; v, = r['off_book']
        self.assertEqual((v['opened'], v['risk_factor']), (T0 + 6 * H, 1.0))
        self.assertEqual(b['risk_factor'], 1.0); self.assertNotIn('live_risk_factor', b)

    def test_live_same_bar_groups_helper(self):
        blk = lambda k: k == 'A'
        pend = [('s', 'A', 1, 4.0), ('s', 'B', 1, 4.0), ('s', 'C', -1, 4.0), ('t', 'D', 1, 0.0)]
        self.assertEqual(X.live_same_bar_groups(pend, set(), {}, {}, blk), {('s', 1): 2, ('s', -1): 1})
        self.assertEqual(X.live_same_bar_groups(pend, {'A'}, {}, {}, blk), {('s', 1): 1, ('s', -1): 1})     # held coin
        self.assertEqual(X.live_same_bar_groups(pend, set(), {'s': 2}, {'s': 1}, blk), {('s', 1): 1})       # A refused (2 + 1 = 3)
        # never below the paper's own groups for the paper entries
        paper = X.same_bar_groups(pend, set(), {'s': 1}, eligible=lambda k: not blk(k))
        live = X.live_same_bar_groups(pend, set(), {'s': 1}, {}, blk)
        self.assertTrue(all(live.get(k, 0) >= n for k, n in paper.items()))


def fix_genes(g):
    return G.fix(dict(g))


class PaperPublishTests(unittest.TestCase):
    """paper.update → view['price_off_live'] / ['price_off_live_exits'] (run.py moves them into signals.json)."""
    COINS = ('A-USDT-SWAP', 'B-USDT-SWAP', 'C-USDT-SWAP')

    def setUp(self):
        gate = mock.patch.object(P, 'entry_gate', lambda node, state=None, streak=None: dict(passed=True, reasons=[]))
        gate.start(); self.addCleanup(gate.stop)
        self.entries, self.revs = {}, {}
        sig = mock.patch.object(X, 'signal_sets', side_effect=lambda g, series: (self.entries.get(int(g['lookback']), {}), self.revs.get(int(g['lookback']), {})))
        sig.start(); self.addCleanup(sig.stop)
        mc = mock.patch.object(P, 'MANUAL_CLOSES', {}); mc.start(); self.addCleanup(mc.stop)

    def sr(self, n):
        ts = [T0 + i * H for i in range(n)]
        return {k: X.Series(k, ts, [100.0] * n, [100.1] * n, [99.99] * n, [100.0] * n) for k in self.COINS}

    def state(self, live_from=T0):
        g = G.random_genome(random.Random(1), 'momentum')
        g.update(direction='both', lookback=11, stop_atr=2.0, target_rr=2.5, risk=.06, leverage=7, vol_min=.3, vol_max=4.0, tf=4)
        nodes = {'n1': dict(id='n1', genes=g, species='momentum', fitness=1.0, qualified=True)}
        acc = dict(from_ts=T0, start_equity=10000.0, next=1, segs=[dict(id='a1', champion='n1', slot=0, genes=fix_genes(g), species='momentum',
                                                                       from_ts=T0, wind_ts=None, boosts=[], reason='carry')],
                   side_cap_from=T0, risk_rules_from=T0, risk_cap_from=T0, exit_rules_from=T0, trail_risk_from=T0, manual_from={}, next_hour_from=T0)
        if live_from is not None:
            acc['price_off_live_from'] = live_from
        return dict(nodes=nodes, team=[dict(id='n1', since=T0)], leader=None, champion='n1', paper=dict(account=acc, start_equity=10000.0))

    def update(self, st, n, price_off):
        return P.update(json.loads(json.dumps(st)), self.sr(n), [], price_off, replay=True)

    def test_only_price_off_blocked_signals_are_published_and_paper_is_unchanged(self):
        self.entries = {11: {5: [('A-USDT-SWAP', 1, 4.0), ('B-USDT-SWAP', -1, 4.0)], 19: [('C-USDT-SWAP', 1, 3.0)]}}
        off = {'A-USDT-SWAP': [[T0, None]], 'C-USDT-SWAP': [[T0, None]]}
        on_v, on_s = self.update(self.state(), 20, off)
        off_v, off_s = self.update(self.state(live_from=None), 20, off)                  # cutover key missing = feature not in effect
        self.assertEqual(on_s, off_s)                                                     # paper mirror signals unchanged
        for k in ('equity', 'positions', 'pending', 'trades', 'curve', 'exits', 'members', 'halted'):
            self.assertEqual(on_v[k], off_v[k], k)
        self.assertEqual(list(on_s), ['B-USDT-SWAP'])                                     # B was not blocked: a normal paper position
        self.assertEqual(off_v['price_off_live'], {})
        pol = on_v['price_off_live']
        self.assertEqual(sorted(pol), ['A-USDT-SWAP', 'C-USDT-SWAP'])                     # A: open virtual; C: queued for the next open
        a, c = pol['A-USDT-SWAP'], pol['C-USDT-SWAP']
        self.assertEqual((a['side'], a['opened'], a['price_off'], a['tf'], a['stop_atr'], a['target_rr']), (1, T0 + 6 * H, True, 4, 2.0, 2.5))
        self.assertEqual((a['risk'], a['leverage'], a['champion'], a['strategy']), (.06, 7, 'n1', 'evolved'))
        self.assertAlmostEqual(a['tp'], a['entry'] + 2.5 * 4.0)
        self.assertTrue(c['pending']); self.assertEqual(c['opened'], T0 + 20 * H)
        self.assertNotIn('B-USDT-SWAP', pol)
        # the cutover: virtual fills only after price_off_live_from
        late_v, _ = self.update(self.state(live_from=T0 + 10 * H), 20, off)
        self.assertEqual(sorted(late_v['price_off_live']), ['C-USDT-SWAP'])

    def test_mixed_entries_queued_and_filled_carry_matching_live_factors(self):
        off = {'A-USDT-SWAP': [[T0, None]]}
        # queued for the next open (signal on the last bar 39; the seat is 40 h old: no new-seat half)
        self.entries = {11: {39: [('A-USDT-SWAP', 1, 4.0), ('B-USDT-SWAP', 1, 4.0)]}}
        on_v, on_s = self.update(self.state(), 40, off)
        off_v, off_s = self.update(self.state(live_from=None), 40, off)
        b, a = on_s['B-USDT-SWAP'], on_v['price_off_live']['A-USDT-SWAP']
        self.assertTrue(b['pending'] and a['pending'])
        self.assertEqual((b['risk_factor'], b['live_risk_factor'], a['risk_factor']), (1.0, .5, .5))
        self.assertEqual({k: x for k, x in b.items() if k != 'live_risk_factor'}, off_s['B-USDT-SWAP'])   # paper signal unchanged
        self.assertNotIn('live_risk_factor', off_s['B-USDT-SWAP'])
        for k in ('equity', 'positions', 'pending', 'trades', 'curve'):
            self.assertEqual(on_v[k], off_v[k], k)
        # the next replay fills them: the same factors on the filled paper position and the virtual one
        v2, s2 = self.update(self.state(), 41, off)
        b2, a2 = s2['B-USDT-SWAP'], v2['price_off_live']['A-USDT-SWAP']
        self.assertFalse(b2.get('pending') or a2.get('pending'))
        self.assertEqual((b2['risk_factor'], b2['live_risk_factor'], a2['risk_factor']), (1.0, .5, .5))
        p, = [x for x in v2['positions'] if x['inst'] == 'B-USDT-SWAP']
        _, s_off = self.update(self.state(live_from=None), 41, off)
        self.assertEqual({k: x for k, x in b2.items() if k != 'live_risk_factor'}, s_off['B-USDT-SWAP'])
        # filled early (bar 6, seat 6 h old: first bar ×½ on both): paper ½, live ¼ + ¼
        self.entries = {11: {5: [('A-USDT-SWAP', 1, 4.0), ('B-USDT-SWAP', 1, 4.0)]}}
        v3, s3 = self.update(self.state(), 20, off)
        self.assertEqual((s3['B-USDT-SWAP']['risk_factor'], s3['B-USDT-SWAP']['live_risk_factor'], v3['price_off_live']['A-USDT-SWAP']['risk_factor']),
                         (.5, .25, .25))
        # no price_off entry on that bar: no live_risk_factor
        self.entries = {11: {39: [('B-USDT-SWAP', 1, 4.0)]}}
        _, s4 = self.update(self.state(), 40, off)
        self.assertNotIn('live_risk_factor', s4['B-USDT-SWAP'])

    def test_virtual_exit_is_published_for_the_live_close(self):
        self.entries = {11: {5: [('A-USDT-SWAP', 1, 4.0)]}}
        self.revs = {11: {15: {'A-USDT-SWAP': -1}}}
        v, _ = self.update(self.state(), 20, {'A-USDT-SWAP': [[T0, None]]})
        self.assertEqual(v['price_off_live'], {})
        self.assertEqual(v['price_off_live_exits'], {f'A-USDT-SWAP|{T0 + 6 * H}': 'reverse_signal'})

    def test_cutover_key_is_stamped_once_on_the_effective_bar(self):
        self.assertIn('price_off_live_from', P.CUTOVER_KEYS)
        st = self.state(live_from=None)
        now = T0 + 20 * H + 30 * 60_000
        P.update(st, self.sr(20), [], {}, now=now)
        self.assertEqual(st['paper']['account']['price_off_live_from'], P.effective_bar(now))
        P.update(st, self.sr(22), [], {}, now=now + 2 * H)
        self.assertEqual(st['paper']['account']['price_off_live_from'], P.effective_bar(now))      # never moved


# ------------------------------------------------------------------------------------------------------------ live
def candles(n, last_open_ms, base=100.0, step=1.0, tf_h=1, confirm='1'):
    """OKX candle rows (newest first) ending with the bar opened at last_open_ms."""
    rows = []
    for k in range(n):
        ts = last_open_ms - (n - 1 - k) * tf_h * H
        o = base + (k % 5) * .1
        rows.append([str(ts), str(o), str(o + step), str(o - step), str(o + .05), '1', '1', '1', confirm])
    return rows[::-1]


def real_atr(rows):
    asc = sorted(rows, key=lambda r: int(r[0]))
    s = X.Series('x', [int(r[0]) for r in asc], *[[float(r[j]) for r in asc] for j in (1, 2, 3, 4)])
    return s.atr()[-1]


class LiveFakeOKX(FakeOKX):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.candle_paths = []

    def request(self, method, path, body=None):
        if path.startswith('/api/v5/market/candles'):
            self.candle_paths.append(path)
        return super().request(method, path, body)


def pol_signal(inst='XRP-USDT-SWAP', side=1, opened=None, stop=1.10, tp=1.60, entry=1.375, tf=1, stop_atr=2.0, target_rr=2.0, **kw):
    """A price_off_live signal whose paper entry / stop / tp are 10 % away from the live price (1.25)."""
    return dict(dict(side=side, entry=entry, stop=stop, tp=tp, dist=abs(entry - stop), trailing=False, opened=opened or NOW_MS - 50 * 60_000,
                     strategy='evolved', champion='c1', rank=2, risk=.05, leverage=5, risk_factor=1.0, seat_from=0, winding=False,
                     price_off=True, tf=tf, stop_atr=stop_atr, target_rr=target_rr), **kw)


def doc(positions=None, pol=None, exits=None, pol_exits=None, with_field=True):
    sig = dict(updated_at='x', positions=positions or {}, exits=exits or {})
    if with_field:
        sig.update(price_off_live=pol or {}, price_off_live_exits=pol_exits or {})
    return sig


class LiveFollowTests(unittest.TestCase):
    GLOBALS = ('INSTRUMENTS', 'RISK', 'SAME_SIDE_CAP', 'ACTIVE_PROFILE', 'DAILY_HALT', 'MARGIN_MODE')

    def setUp(self):
        saved = {k: getattr(odt, k) for k in self.GLOBALS}
        self.addCleanup(lambda: [setattr(odt, k, v) for k, v in saved.items()])
        odt.configure_evolved(['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'XRP-USDT-SWAP'])
        self.opened = NOW_MS - 50 * 60_000                            # fill bar open = signal bar close (1H)

    def fake(self, **kw):
        f = LiveFakeOKX(**kw)
        f.candles = {'XRP-USDT-SWAP': candles(60, self.opened - H, base=1.25, step=.01),
                     'ETH-USDT-SWAP': candles(60, self.opened - H, base=1650.0, step=8.0)}
        return f

    def mirror(self, fake, sig, state=None, live=True, now=NOW):
        state = state if state is not None else dict(odt.new_state(), paused=False)
        paper = {'positions': sig.get('positions', {}), 'exits': sig.get('exits', {})}
        if live:
            paper.update(odt.price_off_live_fields(sig))
        m = odt.Mirror(fake, state, paper, now, False)
        m.follow_price_off = live
        m.run('run')
        return m, state

    def test_live_recomputes_stop_and_target_from_real_candles_not_the_sim_prices(self):
        fake = self.fake()
        m, state = self.mirror(fake, doc(pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened)}))
        paths = [p for p, _ in fake.posts]
        self.assertEqual(paths, ['/api/v5/account/set-leverage', '/api/v5/trade/order'])
        self.assertIn('bar=1H', fake.candle_paths[0])
        atr = real_atr(fake.candles['XRP-USDT-SWAP'])
        px, dist = 1.25, 2.0 * atr
        algo = fake.posts[1][1]['attachAlgoOrds'][0]
        self.assertEqual(algo['slTriggerPx'], odt.fmt(odt.quantize(px - dist, '0.0001', odt.ROUND_HALF_UP)))
        self.assertEqual(algo['tpTriggerPx'], odt.fmt(odt.quantize(px + 2.0 * dist, '0.0001', odt.ROUND_HALF_UP)))
        self.assertNotEqual(float(algo['slTriggerPx']), 1.10); self.assertNotEqual(float(algo['tpTriggerPx']), 1.60)
        rec = state['mirrored'][f'XRP-USDT-SWAP|{self.opened}']
        self.assertTrue(rec['price_off_live']); self.assertEqual((rec['sim_stop'], rec['sim_tp']), (1.10, 1.60))
        self.assertAlmostEqual(rec['stop'], px - dist); self.assertAlmostEqual(rec['target'], px + 2 * dist)
        self.assertAlmostEqual(rec['dist'], dist); self.assertAlmostEqual(rec['real_atr'], atr)
        opened = next(a for a in m.actions if a['action'] == 'open')
        self.assertTrue(opened['price_off_live'])
        # the size is the normal rule at the RECOMPUTED distance: 5 % gene (XRP is a major: × 2 = 10 %) of 5000
        self.assertEqual(fake.posts[1][1]['sz'], odt.fmt(odt.quantize(500 / (px * (dist / px)) / 100, '1')))

    def test_target_rr_below_half_maps_to_the_default_3r_take_profit(self):
        """execution.target_rr (as today): a gene < .5 is DEFAULT_TARGET_RR — every position keeps a take-profit."""
        fake = self.fake()
        self.mirror(fake, doc(pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened, target_rr=0.0)}))
        dist = 2.0 * real_atr(fake.candles['XRP-USDT-SWAP'])
        algo = fake.posts[1][1]['attachAlgoOrds'][0]
        self.assertEqual(algo['tpTriggerPx'], odt.fmt(odt.quantize(1.25 + X.DEFAULT_TARGET_RR * dist, '0.0001', odt.ROUND_HALF_UP)))

    def test_short_whose_target_would_be_below_zero_is_skipped_not_sent(self):
        fake = self.fake()
        sig = pol_signal(side=-1, opened=self.opened, entry=1.25, stop=1.40, tp=0.5, stop_atr=30.0, target_rr=6.0)
        m, state = self.mirror(fake, doc(pol={'XRP-USDT-SWAP': sig}))
        self.assertEqual(fake.posts, [])
        self.assertEqual(m.actions[-1]['reason'], 'target_not_positive')

    def test_atr_uses_only_closed_real_bars_up_to_the_signal_bar_and_fails_closed(self):
        fake = self.fake()
        rows = fake.candles['XRP-USDT-SWAP']
        later = candles(3, self.opened + H, base=9.0, step=3.0)                     # bars after the signal and an open bar: ignored
        fake.candles['XRP-USDT-SWAP'] = later[:2] + [later[2][:8] + ['0']] + rows
        self.mirror(fake, doc(pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened)}))
        algo = fake.posts[1][1]['attachAlgoOrds'][0]
        self.assertAlmostEqual(float(algo['slTriggerPx']), 1.25 - 2 * real_atr(rows), places=4)
        for bad in ([], rows[:10], rows[1:]):                                        # none / too few / signal bar missing
            fake = self.fake(); fake.candles['XRP-USDT-SWAP'] = bad
            m, state = self.mirror(fake, doc(pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened)}))
            self.assertEqual(fake.posts, [])
            self.assertEqual(m.actions[-1]['reason'], 'real_atr_unavailable')

    def test_daily_and_4h_timeframes_request_utc_bars(self):
        for tf, bar in ((24, 'bar=1Dutc'), (4, 'bar=4H')):
            fake = self.fake()
            fake.candles['XRP-USDT-SWAP'] = candles(60, self.opened - tf * H, base=1.25, step=.01, tf_h=tf)
            self.mirror(fake, doc(pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened, tf=tf)}))
            self.assertIn(bar, fake.candle_paths[0])
            self.assertEqual(len(fake.posts), 2)

    def test_demo_never_follows_the_list(self):
        fake = self.fake()
        m, state = self.mirror(fake, doc(pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened)}), live=False)
        self.assertEqual(fake.posts, []); self.assertEqual(state['mirrored'], {}); self.assertEqual(fake.candle_paths, [])
        # even if the field reached a demo mirror's paper dict, open() refuses without follow_price_off
        m = odt.Mirror(self.fake(), dict(odt.new_state(), paused=False), {'positions': {}}, NOW, False)
        m.load()
        self.assertIsNone(m.real_levels('XRP-USDT-SWAP', pol_signal(), 1.25))
        self.assertEqual(m.actions[-1]['reason'], 'price_off_live_not_live')

    def test_old_signals_without_the_field_behave_exactly_as_before(self):
        normal = {'BTC-USDT-SWAP': dict(side=1, entry=70000.0, stop=68600.0, tp=None, opened=self.opened, strategy='evolved', leverage=7, risk=.05, rank=1)}
        a, b = self.fake(), self.fake()
        ma, sa = self.mirror(a, doc(positions=normal, with_field=False), live=True)
        mb, sb = self.mirror(b, doc(positions=normal, with_field=False), live=False)
        self.assertEqual(a.posts, b.posts); self.assertEqual(sa, sb); self.assertEqual(a.candle_paths, [])
        self.assertEqual(odt.price_off_live_fields({})['price_off_live'], None)

    def test_manual_eth_short_blocks_a_price_off_eth_signal_and_is_never_touched(self):
        eth = dict(instId='ETH-USDT-SWAP', posSide='net', pos='-7.41', avgPx='1687.04', markPx='1651.35', upl='26.4', lever='100', mgnMode='cross')
        fake = self.fake(positions=[eth], pos_mode='net_mode', prices={'ETH-USDT-SWAP': 1651.35, 'XRP-USDT-SWAP': 1.25, 'BTC-USDT-SWAP': 70000.0})
        sig = pol_signal('ETH-USDT-SWAP', side=-1, opened=self.opened, entry=1820.0, stop=1900.0, tp=1600.0)
        m, state = self.mirror(fake, doc(pol={'ETH-USDT-SWAP': sig}))
        self.assertEqual(fake.posts, [])                                                # no order, no close, no algo on ETH
        skip = [a for a in m.actions if a['inst'] == 'ETH-USDT-SWAP']
        self.assertEqual([(a['action'], a['reason']) for a in skip], [('skip', 'exchange_position_exists_not_ours')])
        self.assertTrue(skip[0]['price_off_live'])
        rec = state['mirrored'][f'ETH-USDT-SWAP|{self.opened}']
        self.assertEqual(rec['status'], 'skipped')
        self.assertEqual(fake.candle_paths, [])                                         # never got as far as recomputing levels
        # later runs: deduped, still nothing sent; the manual position stays as it is
        m, state = self.mirror(fake, doc(pol={'ETH-USDT-SWAP': sig}), state=state)
        self.assertEqual(fake.posts, []); self.assertEqual([a for a in m.actions if a['inst'] == 'ETH-USDT-SWAP'], [])

    def test_idempotent_by_inst_and_opened(self):
        fake = self.fake()
        sig = doc(pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened)})
        _, state = self.mirror(fake, sig)
        self.assertEqual(len(fake.posts), 2)
        fake.positions = [dict(instId='XRP-USDT-SWAP', posSide='long', pos='1')]
        fake.algos = [dict(instId='XRP-USDT-SWAP', posSide='long', algoId='a1', slTriggerPx=str(state['mirrored'][f'XRP-USDT-SWAP|{self.opened}']['stop']),
                           tpTriggerPx=str(state['mirrored'][f'XRP-USDT-SWAP|{self.opened}']['target']))]
        fake.posts.clear()
        self.mirror(fake, sig, state=state, now=NOW + timedelta(minutes=5))
        self.assertEqual([p for p, _ in fake.posts if p != '/api/v5/trade/amend-algos'], [])   # never a second order
        self.assertFalse(any(b.get('newTpTriggerPx') for p, b in fake.posts))                  # TP never moved to the sim tp

    def _open_one(self):
        fake = self.fake()
        sig = doc(pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened)})
        _, state = self.mirror(fake, sig)
        rec = state['mirrored'][f'XRP-USDT-SWAP|{self.opened}']
        fake.positions = [dict(instId='XRP-USDT-SWAP', posSide='long', pos=fake.posts[1][1]['sz'], avgPx='1.25')]
        fake.algos = [dict(instId='XRP-USDT-SWAP', posSide='long', algoId='a1', slTriggerPx=str(rec['stop']), tpTriggerPx=str(rec['target']))]
        fake.posts.clear()
        return fake, state, rec

    def test_strategy_exit_closes_the_live_position_with_the_published_reason(self):
        fake, state, rec = self._open_one()
        key = f'XRP-USDT-SWAP|{self.opened}'
        m, state = self.mirror(fake, doc(pol={}, pol_exits={key: 'reverse_signal'}), state=state, now=NOW + timedelta(hours=1))
        self.assertEqual(fake.posts[0][0], '/api/v5/trade/close-position')
        self.assertEqual(state['mirrored'][key]['closed_reason'], 'reverse_signal')
        close = next(a for a in m.actions if a['action'] == 'close')
        self.assertTrue(close['price_off_live'])

    def test_virtual_stop_or_target_closes_as_paper_closed(self):
        fake, state, rec = self._open_one()
        key = f'XRP-USDT-SWAP|{self.opened}'
        self.mirror(fake, doc(pol={}, pol_exits={key: 'stop'}), state=state, now=NOW + timedelta(hours=1))
        self.assertEqual(state['mirrored'][key]['closed_reason'], 'paper_closed')

    def test_missing_field_or_invalid_entry_never_closes_and_keeps_the_exchange_orders(self):
        fake, state, rec = self._open_one()
        key = f'XRP-USDT-SWAP|{self.opened}'
        for sig in (doc(with_field=False), doc(pol={'XRP-USDT-SWAP': dict(side=1)})):          # older publisher / malformed entry
            self.mirror(fake, sig, state=state, now=NOW + timedelta(hours=1))
            self.assertFalse(any(p == '/api/v5/trade/close-position' for p, _ in fake.posts))
            self.assertEqual(state['mirrored'][key]['status'], 'open')
        # exchange stop hit → closed_on_exchange as for any mirror
        fake.positions, fake.algos = [], []
        m, state = self.mirror(fake, doc(pol={}), state=state, now=NOW + timedelta(hours=2))
        self.assertEqual(state['mirrored'][key]['status'], 'done')
        self.assertEqual(m.actions[0]['action'], 'closed_on_exchange')

    def test_trailing_uses_the_live_levels_not_the_sim_stop(self):
        fake, state, rec = self._open_one()
        key = f'XRP-USDT-SWAP|{self.opened}'
        # the sim stop (1.10) is far below; a huge sim "tighter" stop must not be used either
        sig = doc(pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened, stop=1.249, tp=1.26, trailing=True)})
        self.mirror(fake, sig, state=state, now=NOW + timedelta(hours=1))
        self.assertFalse(any(b.get('newSlTriggerPx') == '1.249' for _, b in fake.posts))
        self.assertFalse(any(p == '/api/v5/trade/close-position' for p, _ in fake.posts))
        self.assertEqual(state['mirrored'][key]['status'], 'open')

    def test_paper_signals_first_and_one_position_per_coin(self):
        fake = self.fake()
        normal = {'XRP-USDT-SWAP': dict(side=1, entry=1.25, stop=1.22, tp=None, opened=self.opened - H, strategy='evolved', leverage=5, risk=.05, rank=1)}
        m, state = self.mirror(fake, doc(positions=normal, pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened)}))
        orders = [b for p, b in fake.posts if p == '/api/v5/trade/order']
        self.assertEqual(len(orders), 1); self.assertEqual(orders[0]['attachAlgoOrds'][0]['slTriggerPx'], '1.22')
        self.assertEqual(state['mirrored'][f'XRP-USDT-SWAP|{self.opened}']['reason'], 'coin_already_mirrored')

    def test_mixed_same_bar_entries_live_splits_demo_keeps_full_risk(self):
        normal = {'BTC-USDT-SWAP': dict(side=1, entry=70000.0, stop=68600.0, tp=None, opened=self.opened, strategy='evolved', leverage=7,
                                        risk=.05, rank=1, champion='c1', risk_factor=1.0, live_risk_factor=.5)}
        pol = {'XRP-USDT-SWAP': pol_signal(opened=self.opened, risk_factor=.5)}
        fake = self.fake()
        m, state = self.mirror(fake, doc(positions=normal, pol=pol), live=True)
        k_btc, k_xrp = f'BTC-USDT-SWAP|{self.opened}', f'XRP-USDT-SWAP|{self.opened}'
        self.assertEqual((state['mirrored'][k_btc]['status'], state['mirrored'][k_xrp]['status']), ('open', 'open'))
        self.assertEqual((state['mirrored'][k_btc]['risk_factor'], state['mirrored'][k_xrp]['risk_factor']), (.5, .5))   # one trade's risk
        live_risk = {a['inst']: a['risk_usdt'] for a in m.actions if 'risk_usdt' in a}
        fake = self.fake()                                               # demo: only the paper entry, at its full paper factor
        m, state = self.mirror(fake, doc(positions=normal, pol=pol), live=False)
        self.assertEqual(list(state['mirrored']), [k_btc])
        self.assertEqual(state['mirrored'][k_btc]['risk_factor'], 1.0)
        demo_risk = {a['inst']: a['risk_usdt'] for a in m.actions if 'risk_usdt' in a}
        self.assertAlmostEqual(live_risk['BTC-USDT-SWAP'], demo_risk['BTC-USDT-SWAP'] / 2, places=1)
        # live without live_risk_factor (older publisher): risk_factor as before
        fake = self.fake()
        m, state = self.mirror(fake, doc(positions={'BTC-USDT-SWAP': {k: v for k, v in normal['BTC-USDT-SWAP'].items() if k != 'live_risk_factor'}}), live=True)
        self.assertEqual(state['mirrored'][k_btc]['risk_factor'], 1.0)

    def test_too_old_entry_uses_late_ok_and_the_daily_halt_blocks(self):
        fake = self.fake(prices={'XRP-USDT-SWAP': 1.10, 'BTC-USDT-SWAP': 70000.0})   # price ran to the sim stop: late_ok fails
        old = self.opened - 6 * H
        fake.candles['XRP-USDT-SWAP'] = candles(60, old - H, base=1.25, step=.01)
        m, state = self.mirror(fake, doc(pol={'XRP-USDT-SWAP': pol_signal(opened=old)}))
        self.assertEqual(fake.posts, []); self.assertEqual(m.actions[-1]['reason'], 'paper_entry_too_old')
        fake = self.fake()
        st = dict(odt.new_state(), paused=False, halted=True, day=odt.local_day(NOW))
        self.mirror(fake, doc(pol={'XRP-USDT-SWAP': pol_signal(opened=self.opened)}), state=st)
        self.assertEqual(fake.posts, [])

    def test_field_validation(self):
        good = pol_signal(opened=self.opened)
        f = odt.price_off_live_fields(dict(price_off_live={'XRP-USDT-SWAP': good, 'BTC-USDT-SWAP': dict(good, tf=2), 'bad': good},
                                           price_off_live_exits={'a|1': 'stop', 'b|2': 3}))
        self.assertEqual(list(f['price_off_live']), ['XRP-USDT-SWAP'])
        self.assertEqual(f['price_off_live_invalid'], {'BTC-USDT-SWAP', 'bad'})
        self.assertEqual(f['price_off_live_exits'], {'a|1': 'stop'})
        self.assertIsNone(odt.price_off_live_fields(dict(price_off_live=[1]))['price_off_live'])


EXTRA = r'''
if path.startswith('/api/v5/market/candles'):
    rows = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'candles.json')))
    out({'code': '0', 'data': rows})
'''


class MainWiringTests(LiveModeBase):
    """main(): only OKX_TRADING_MODE=live follows price_off_live; demo ignores it (fake curl, no network)."""
    def setUp(self):
        super().setUp()
        head, sep, tail = FAKE_CURL.rpartition("out({'code': '0', 'data': []})\n")   # before the final catch-all answer
        script = head + EXTRA + sep + tail
        (self.bin / 'curl').write_text(script)
        now = datetime.now(timezone.utc)
        self.opened = int(now.timestamp() * 1000) // H * H
        (self.bin / 'candles.json').write_text(json.dumps(candles(60, self.opened - H, base=70000.0, step=300.0)))
        sig = dict(updated_at=now.isoformat(), champion='c1', team=['c1'], instruments=['BTC-USDT-SWAP'], positions={}, exits={},
                   price_off_live={'BTC-USDT-SWAP': dict(pol_signal('BTC-USDT-SWAP', opened=self.opened, entry=77000.0, stop=75000.0, tp=81000.0, leverage=20))},
                   price_off_live_exits={})
        self.write_signals(sig)

    def posts_after_resume(self, **env):
        self.main(**env, OKX_DRY_RUN='0'); self.requests()
        mode = 'live' if env.get('OKX_TRADING_MODE') == 'live' else 'demo'
        self.resume(mode)
        text, code = self.main(**env, OKX_DRY_RUN='0')
        self.assertIsNone(code, text)
        reqs = self.requests()
        return [r for r in reqs if r['method'] == 'POST'], reqs

    def test_live_follows_demo_ignores(self):
        posts, reqs = self.posts_after_resume(OKX_TRADING_MODE='live')
        self.assertEqual([p['path'] for p in posts], ['/api/v5/account/set-leverage', '/api/v5/trade/order'])
        algo = json.loads(posts[1]['body'])['attachAlgoOrds'][0]
        self.assertNotEqual(algo['slTriggerPx'], '75000')                                # recomputed at the live price 70000
        self.assertLess(float(algo['slTriggerPx']), 70000.0)
        self.assertTrue(any('/market/candles' in r['path'] and 'x-simulated-trading' not in r['headers'] for r in reqs))
        posts, reqs = self.posts_after_resume()                                          # demo
        self.assertEqual(posts, [])
        self.assertFalse(any('/market/candles' in r['path'] for r in reqs))


if __name__ == '__main__':
    unittest.main()
