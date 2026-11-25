"""MARGIN SIZING (2026-11-25, tools/evolve/sizing.py): margin = base equity × m (risk gene 5–10 % → 2–10 %, majors × 2,
crowding half at 10, clamped to 2–10 %, then × risk_factor), notional = margin × leverage. Paper, the evolution's
simulate() and the OKX follower change together; the paper switches on account['margin_sizing_from'] (NEXT-HOUR cutover
key) and marks the signals it sized by margin (sizing='margin'), which are the only ones the follower sizes by margin.
Nothing here reaches OKX: fake clients only.
"""
import json
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'evolve'))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import execution as X        # noqa: E402
import paper as P            # noqa: E402
import sizing as SZ          # noqa: E402
import okx_demo_trader as odt  # noqa: E402
from test_okx_demo_trader import FakeOKX, NOW_MS, run, paper  # noqa: E402
from test_evolve import base_genes, fix_genes, T0, H  # noqa: E402
from test_price_off_live import LiveFakeOKX, pol_signal, doc, candles  # noqa: E402

ALT = 'ALT-USDT-SWAP'
ALT_SPEC = dict(ctVal='1', lotSz='0.01', minSz='0.01', tickSz='0.0001', lever='50')


# ------------------------------------------------------------------------------------------------------------ formula
class FormulaTests(unittest.TestCase):
    def test_user_examples_margin_and_notional(self):
        # 本金 100 × 5 % = 保证金 5，× 20 = 仓位 100；本金 200 → 仓位 200
        for base, want in ((100.0, 100.0), (200.0, 200.0)):
            qty = SZ.margin_qty(base, .05, 20, 1.0)
            self.assertAlmostEqual(qty * 1.0, want)                 # notional at price 1
            self.assertAlmostEqual(qty * 1.0 / 20, base * .05)      # margin
        self.assertAlmostEqual(SZ.margin_qty(100.0, .05, 20, 50.0) * 50.0, 100.0)   # the price only sets the quantity
        for bad in ((0, .05, 20, 1), (100, 0, 20, 1), (100, .05, 0, 1), (100, .05, 20, 0), (-5, .05, 20, 1)):
            self.assertEqual(SZ.margin_qty(*bad), 0.0)

    def test_gene_maps_linearly_from_5_10_to_2_10_and_out_of_band_genes_are_clamped(self):
        for gene, want in ((.05, .02), (.075, .06), (.10, .10), (.0625, .04), (.03, .02), (.004, .02), (None, .02), (.12, .10), (.20, .10)):
            self.assertAlmostEqual(SZ.margin_base(gene), want, msg=gene)
        self.assertEqual((SZ.GENE_BAND, SZ.MARGIN_RANGE), ((.05, .10), (.02, .10)))

    def test_majors_crowding_clamp_then_risk_factor(self):
        mf = SZ.margin_fraction
        self.assertAlmostEqual(mf(.05, major=True), .04)                     # 2 % × 2
        self.assertAlmostEqual(mf(.075, major=True), .10)                    # 6 % × 2 = 12 % → 10 %
        self.assertAlmostEqual(mf(.10, crowd=.5), .05)                       # 10 % × ½
        self.assertAlmostEqual(mf(.05, crowd=.5), .02)                       # 1 % → clamped up to 2 %
        self.assertAlmostEqual(mf(.10, major=True, crowd=.5), .10)           # 10 % × 2 × ½
        self.assertAlmostEqual(mf(.05, factor=.5), .01)                      # risk_factor AFTER the clamp: below 2 % allowed
        self.assertAlmostEqual(mf(.05, crowd=.5, factor=.25), .005)
        self.assertAlmostEqual(mf(.10, major=True, factor=.5), .05)
        self.assertEqual(mf(.10, factor=0.0), 0.0)
        self.assertEqual(SZ.STOP_RISK_SIZING, dict(min_risk=.05, max_risk=.10, crowd_steps=((10, .5),)))   # frozen, == today's rules
        self.assertEqual((X.MIN_TRADE_RISK, X.MAX_TRADE_RISK, X.CROWD_STEPS), (.05, .10, ((10, .5),)))

    def test_mark_margin_marks_only_fills_after_the_stamp(self):
        sig = {'A': dict(opened=100), 'B': dict(opened=200), 'C': dict(opened=201)}
        SZ.mark_margin(sig, None)
        self.assertFalse(any('sizing' in s for s in sig.values()))           # pending key: nothing marked
        SZ.mark_margin(sig, 200)
        self.assertEqual({k: s.get('sizing') for k, s in sig.items()}, {'A': None, 'B': None, 'C': 'margin'})

    def test_live_base_equity_excludes_foreign_upl_in_both_signs(self):
        ct = {'ETH-USDT-SWAP': .1, ALT: 1.0}.get
        eth = dict(instId='ETH-USDT-SWAP', posSide='short', pos='7.41', avgPx='1687.04', markPx='1634.36', upl='39.03588')
        own = dict(instId=ALT, posSide='long', pos='10', avgPx='10', markPx='11', upl='10')
        pos = {('ETH-USDT-SWAP', 'short'): eth, (ALT, 'long'): own}
        base, foreign, why = SZ.live_base_equity(209.044727, pos, {(ALT, 'long')}, ct)
        self.assertIsNone(why); self.assertAlmostEqual(foreign, 39.03588); self.assertAlmostEqual(base, 170.008847)   # our own upl stays in
        base, foreign, _ = SZ.live_base_equity(160.0, {('ETH-USDT-SWAP', 'short'): dict(eth, upl='-40')}, set(), ct)
        self.assertAlmostEqual(base, 200.0)                                  # a manual loss does not shrink our size
        base, _, _ = SZ.live_base_equity(30.0, {('ETH-USDT-SWAP', 'short'): dict(eth, upl='39.03588')}, set(), ct)
        self.assertEqual(base, 0.0)                                          # floored at 0
        # upl missing: recomputed from mark / average / ctVal (short: (1634.36 − 1687.04) × 7.41 × 0.1 × −1)
        base, foreign, _ = SZ.live_base_equity(209.044727, {('ETH-USDT-SWAP', 'short'): dict(eth, upl='')}, set(), ct)
        self.assertAlmostEqual(foreign, (1634.36 - 1687.04) * 7.41 * .1 * -1)
        net = dict(instId='ETH-USDT-SWAP', posSide='net', pos='-2', avgPx='100', markPx='90')
        self.assertAlmostEqual(SZ.live_base_equity(100.0, {('ETH-USDT-SWAP', 'net'): net}, set(), ct)[1], 2.0)   # net short
        # neither readable: no guess
        base, foreign, why = SZ.live_base_equity(100.0, {('ETH-USDT-SWAP', 'short'): dict(eth, upl=None, markPx=None)}, set(), ct)
        self.assertEqual((base, foreign), (None, None)); self.assertTrue(why.startswith('upl_unknown'))
        self.assertEqual(SZ.live_base_equity('x', {}, set(), ct)[2], 'equity_unknown')
        self.assertEqual(SZ.live_base_equity(100.0, {}, set(), ct)[0], 100.0)


# ------------------------------------------------------------------------------------------------------------ live
def alt_signal(**kw):
    return dict(dict(strategy='evolved', side=1, entry=10.0, stop=9.5, tp=12.0, leverage=20, opened=NOW_MS - 10 * 60_000, rank=2, risk=.075), **kw)


def manual_eth(upl):
    return dict(instId='ETH-USDT-SWAP', posSide='short', pos='7.41', avgPx='1687.04', markPx='1634.36', upl=str(upl), lever='100',
                margin='12.1', mgnMode='cross')


class LiveMarginTests(unittest.TestCase):
    GLOBALS = ('INSTRUMENTS', 'RISK', 'SAME_SIDE_CAP', 'ACTIVE_PROFILE', 'DAILY_HALT', 'MARGIN_MODE')

    def setUp(self):
        saved = {k: getattr(odt, k) for k in self.GLOBALS}
        self.addCleanup(lambda: [setattr(odt, k, v) for k, v in saved.items()])
        self.coins = [ALT, 'ETH-USDT-SWAP', 'BTC-USDT-SWAP'] + [f'C{i}-USDT-SWAP' for i in range(22)]
        odt.configure_evolved(self.coins)
        self.specs = {c: dict(ALT_SPEC) for c in self.coins}
        self.specs['ETH-USDT-SWAP'] = dict(ctVal='0.1', lotSz='0.01', minSz='0.01', tickSz='0.01', lever='100')
        self.specs['BTC-USDT-SWAP'] = dict(ctVal='0.01', lotSz='0.01', minSz='0.01', tickSz='0.1', lever='100')

    def fake(self, equity=1000, positions=(), **kw):
        px = {c: 10.0 for c in self.coins}; px.update({'ETH-USDT-SWAP': 1634.36, 'BTC-USDT-SWAP': 70000.0})
        return FakeOKX(equity=equity, positions=positions, prices=px, instruments=self.specs, **kw)

    def orders(self, fake):
        return [b for q, b in fake.posts if q == '/api/v5/trade/order']

    def test_signal_without_marker_keeps_stop_risk_sizing_with_marker_uses_margin(self):
        fake = self.fake(); m, _ = run(fake, paper(**{ALT: alt_signal()}))
        self.assertEqual(self.orders(fake)[0]['sz'], '150')                  # 7.5 % × 1000 = 75 U at a 0.5 stop → 150
        fake = self.fake(); m, st = run(fake, paper(**{ALT: alt_signal(sizing='margin')}))
        self.assertEqual(self.orders(fake)[0]['sz'], '120')                  # m 6 % → margin 60 × 20 = 1200 U → 120
        o = next(a for a in m.actions if a['action'] == 'open')
        self.assertAlmostEqual(o['risk_usdt'], 120 * .5)                     # logged: what its stop loses (6 % here)
        self.assertEqual(o['leverage'], 20)
        rec = next(iter(st['mirrored'].values()))
        self.assertAlmostEqual(float(rec['contracts']) * 10.0 / rec['leverage'], 60.0)   # margin = base × m
        fake = self.fake(); run(fake, paper(**{ALT: alt_signal(sizing='stop_risk')}))
        self.assertEqual(self.orders(fake)[0]['sz'], '150')                  # any other value: the old rule

    def test_user_example_equity_100_and_200(self):
        for eq, sz in ((100, '10'), (200, '20')):                            # m pinned at 5 %, 20×: margin 5 / 10 U
            fake = self.fake(equity=eq)
            with mock.patch.object(odt, 'margin_fraction', lambda *a, **k: .05):
                run(fake, paper(**{ALT: alt_signal(sizing='margin', leverage=20)}))
            self.assertEqual(self.orders(fake)[0]['sz'], sz)                 # notional 100 / 200 U at price 10
            self.assertAlmostEqual(float(sz) * 10.0, eq * .05 * 20)

    def test_leverage_is_min_of_gene_20_and_coin_max(self):
        for gene, coin_max, want in ((50, '50', 20), (7, '50', 7), (20, '5', 5), (None, '50', 10)):
            self.specs[ALT] = dict(ALT_SPEC, lever=coin_max)
            fake = self.fake()
            run(fake, paper(**{ALT: alt_signal(sizing='margin', leverage=gene)}))
            lev = next(b for q, b in fake.posts if q == '/api/v5/account/set-leverage')['lever']
            self.assertEqual(lev, str(want), (gene, coin_max))
            self.assertAlmostEqual(float(self.orders(fake)[0]['sz']), 1000 * .06 * want / 10.0, delta=.011)

    def test_base_excludes_manual_eth_upl_and_eth_is_still_skipped(self):
        for eq, upl in ((1039.04, 39.04), (960.0, -40.0)):
            fake = self.fake(equity=eq, positions=[manual_eth(upl)])
            m, _ = run(fake, paper(**{ALT: alt_signal(sizing='margin'), 'ETH-USDT-SWAP': dict(alt_signal(sizing='margin'), entry=1634.36, stop=1600.0, tp=1700.0)}))
            self.assertEqual([o['instId'] for o in self.orders(fake)], [ALT])
            self.assertAlmostEqual(float(self.orders(fake)[0]['sz']), 1000 * .06 * 20 / 10.0, delta=.011)   # base 1000, not eq
            self.assertIn('exchange_position_exists_not_ours', [a.get('reason') for a in m.actions if a['inst'] == 'ETH-USDT-SWAP'])
        # the old (unmarked) rule still sizes on the full exchange equity
        fake = self.fake(equity=1039.04, positions=[manual_eth(39.04)]); run(fake, paper(**{ALT: alt_signal()}))
        self.assertEqual(self.orders(fake)[0]['sz'], odt.fmt(odt.quantize(.075 * 1039.04 / .5, '0.01')))
        # 2026-11-25 14:58Z the manual ETH short was closed (trailing stop): no position left (live-data 19:50Z: eq 202.26358,
        # positions []) → base = eq, and an ETH margin-sized signal is no longer skipped and sizes on 202.26358
        eq = 202.26358
        self.assertEqual(SZ.live_base_equity(eq, {}, set(), lambda i: .1), (eq, 0.0, None))
        fake = self.fake(equity=eq, positions=[])
        eth = dict(alt_signal(sizing='margin'), entry=1634.36, stop=1600.0, tp=1700.0)    # gene 7.5 % → 6 % × 2 (major) → 10 %, 20×
        m, _ = run(fake, paper(**{'ETH-USDT-SWAP': eth}))
        self.assertEqual([o['instId'] for o in self.orders(fake)], ['ETH-USDT-SWAP'])
        self.assertNotIn('exchange_position_exists_not_ours', [a.get('reason') for a in m.actions])
        self.assertEqual(self.orders(fake)[0]['sz'], odt.fmt(odt.quantize(eq * .10 * 20 / 1634.36 / .1, '0.01')))   # 2.47 contracts (0.2475 ETH, notional ≈ 404.5 U)
        self.assertAlmostEqual(float(self.orders(fake)[0]['sz']) * .1 * 1634.36 / 20, eq * .10, delta=.082)          # margin 20.18 ≈ 20.23 U (within one 0.01 lot)

    def test_own_mirror_upl_stays_in_the_base_and_unknown_foreign_upl_skips(self):
        mine = dict(instId='C0-USDT-SWAP', posSide='long', pos='5', avgPx='10', markPx='30', upl='100', lever='20', mgnMode='cross')
        def with_mirror():
            st = dict(odt.new_state(), paused=False)
            st['mirrored']['C0-USDT-SWAP|1'] = dict(inst='C0-USDT-SWAP', side=1, posSide='long', status='open', stop=9.0, target=40.0, contracts='5',
                                                    entry=10.0, init_stop=9.0, dist=1.0, best=10.0, leverage=20, strategy='evolved', opened_at=NOW_MS - H, mgnMode='cross')
            return st
        state = with_mirror()
        fake = self.fake(equity=1100, positions=[mine])
        sig = {ALT: alt_signal(sizing='margin'), 'C0-USDT-SWAP': dict(alt_signal(), opened=1, entry=10.0, stop=9.0, tp=40.0)}
        run(fake, paper(**sig), state=state)
        self.assertAlmostEqual(float(self.orders(fake)[0]['sz']), 1100 * .06 * 20 / 10.0, delta=.011)    # our upl is ours
        # the same position closed by the paper in this run: still ours for this run's base
        fake = self.fake(equity=1100, positions=[mine]); state = with_mirror()
        run(fake, paper(**{ALT: alt_signal(sizing='margin')}), state=state)
        self.assertEqual(state['mirrored']['C0-USDT-SWAP|1']['status'], 'done')
        self.assertAlmostEqual(float(self.orders(fake)[0]['sz']), 1100 * .06 * 20 / 10.0, delta=.011)
        # a manual position whose upl cannot be read: the margin-sized entry is skipped (an unmarked one is not)
        bad = dict(manual_eth(0), upl='', markPx='')
        fake = self.fake(equity=1000, positions=[bad]); m, _ = run(fake, paper(**{ALT: alt_signal(sizing='margin')}))
        self.assertEqual(self.orders(fake), [])
        self.assertEqual([a.get('reason') for a in m.actions if a['inst'] == ALT], ['base_equity_unknown'])
        fake = self.fake(equity=1000, positions=[bad]); run(fake, paper(**{ALT: alt_signal()}))
        self.assertEqual(len(self.orders(fake)), 1)

    def test_majors_crowding_and_risk_factor_on_live(self):
        fake = self.fake(); run(fake, paper(**{'BTC-USDT-SWAP': dict(alt_signal(sizing='margin', risk=.05, leverage=10), entry=70000.0, stop=68000.0, tp=80000.0)}))
        self.assertEqual(self.orders(fake)[0]['sz'], '0.57')                 # 2 % × 2 = 4 % × 1000 × 10 / 70000 / 0.01 = 0.571
        fake = self.fake(); run(fake, paper(**{ALT: alt_signal(sizing='margin', risk=.05, risk_factor=.25)}))
        self.assertEqual(self.orders(fake)[0]['sz'], '10')                   # 2 % × ¼ = 0.5 % (below 2 %) × 1000 × 20 / 10
        coins = [f'C{i}-USDT-SWAP' for i in range(11)]
        sig = {c: alt_signal(sizing='margin', risk=.075, leverage=2, side=-1, stop=10.5, tp=8.0, rank=r) for r, c in enumerate(coins)}
        fake = self.fake(equity=100000); run(fake, paper(**sig))
        self.assertEqual([o['sz'] for o in self.orders(fake)], ['1200'] * 10 + ['600'])    # 6 % then ½ at the 11th (3 %)
        # at m 10 % the 95 % margin cap binds before the 10th position, so crowding is never reached there
        sig = {c: dict(s, risk=.10) for c, s in sig.items()}
        fake = self.fake(equity=100000); run(fake, paper(**sig))
        self.assertEqual([o['sz'] for o in self.orders(fake)], ['2000'] * 9)

    def test_live_risk_factor_and_price_off_live_go_through_the_margin_formula(self):
        # a paper signal with live_risk_factor: the live follower (follow_price_off) uses it, demo keeps risk_factor
        for follow, sz in ((True, '60'), (False, '120')):
            fake = self.fake()
            m = odt.Mirror(fake, dict(odt.new_state(), paused=False), paper(**{ALT: alt_signal(sizing='margin', risk_factor=1.0, live_risk_factor=.5)}), odt.datetime.fromtimestamp(NOW_MS / 1000, odt.timezone.utc), False)
            m.follow_price_off = follow; m.run('run')
            self.assertEqual(self.orders(fake)[0]['sz'], sz)
        # PRICE_OFF LIVE entry (XRP, a major, live levels from real candles): m = clamp(6 % × 2) × risk_factor ½ = 5 %
        odt.configure_evolved(['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'XRP-USDT-SWAP'])
        opened = NOW_MS - 50 * 60_000
        for marker, want in (('margin', 5000 * .05 * 5 / 1.25 / 100), (None, None)):
            f = LiveFakeOKX()
            f.candles = {'XRP-USDT-SWAP': candles(60, opened - H, base=1.25, step=.01)}
            sig = pol_signal(opened=opened, risk=.075, leverage=5, risk_factor=.5, **({'sizing': marker} if marker else {}))
            pap = {'positions': {}, 'exits': {}}; pap.update(odt.price_off_live_fields(doc(pol={'XRP-USDT-SWAP': sig})))
            m = odt.Mirror(f, dict(odt.new_state(), paused=False), pap, odt.datetime.fromtimestamp(NOW_MS / 1000, odt.timezone.utc), False)
            m.follow_price_off = True; m.run('run')
            o = [b for q, b in f.posts if q == '/api/v5/trade/order']
            self.assertEqual(len(o), 1)
            if want is not None:
                self.assertEqual(o[0]['sz'], odt.fmt(odt.quantize(want, '1')))   # 5000 × 5 % × 5 / 1.25 / 100 = 10
            else:                                                             # no marker: the stop-risk size at the live stop
                stop = next(iter(m.s['mirrored'].values()))['stop']
                self.assertEqual(o[0]['sz'], odt.fmt(odt.quantize(5000 * .10 * .5 / (1.25 - stop) / 100, '1')))

    def test_no_target_short_is_margin_sized_and_opens_with_the_stop_only(self):
        # NEGATIVE TP RULE (PR #118) × MARGIN SIZING: the missing take-profit changes neither the size nor the leverage
        szs = {}
        for name, extra in (('valid_tp', dict(tp=8.0)), ('no_target', dict(tp=None, no_target=True)), ('negative_tp', dict(tp=-3.0))):
            fake = self.fake()
            m, st = run(fake, paper(**{ALT: alt_signal(sizing='margin', side=-1, entry=10.0, stop=14.0, **extra)}))
            o = self.orders(fake)[0]
            szs[name] = o['sz']
            algo = o['attachAlgoOrds'][0]
            self.assertEqual(algo['slTriggerPx'], '14')
            rec = next(iter(st['mirrored'].values()))
            if name == 'valid_tp':
                self.assertEqual(algo['tpTriggerPx'], '8'); self.assertNotIn('no_target', rec)
            else:
                self.assertNotIn('tpTriggerPx', algo); self.assertEqual((rec['target'], rec['no_target']), (None, True))
            self.assertAlmostEqual(next(a for a in m.actions if a['action'] == 'open')['risk_usdt'], float(o['sz']) * 4.0)
        self.assertEqual(set(szs.values()), {'120'})                         # 6 % × 1000 × 20 ÷ 10, whatever the target

    def test_unchanged_gates_95_percent_margin_same_side_20_min_size_and_daily_halt(self):
        # 95 % of the available margin: m 10 % at 1× → 100 U each; the 10th no longer fits (95 % of the last 100 U)
        coins = [f'C{i}-USDT-SWAP' for i in range(12)]
        sig = {c: alt_signal(sizing='margin', risk=.10, leverage=1, rank=r) for r, c in enumerate(coins)}
        fake = self.fake(equity=1000); m, _ = run(fake, paper(**sig))
        self.assertEqual(len(self.orders(fake)), 9)
        self.assertIn('insufficient_margin', [a.get('reason') for a in m.actions])
        # same-side count of 20
        coins = [f'C{i}-USDT-SWAP' for i in range(22)]
        sig = {c: alt_signal(sizing='margin', risk=.05, leverage=1, rank=r) for r, c in enumerate(coins)}
        fake = self.fake(equity=100000); m, _ = run(fake, paper(**sig))
        self.assertEqual(len(self.orders(fake)), 20)
        self.assertEqual([a.get('reason') for a in m.actions if a['action'] == 'skip'], ['same_side_count'] * 2)
        # minSz
        self.specs[ALT] = dict(ALT_SPEC, lotSz='1', minSz='5')
        fake = self.fake(equity=30); m, _ = run(fake, paper(**{ALT: alt_signal(sizing='margin', risk=.05, leverage=1)}))
        self.assertEqual(self.orders(fake), []); self.assertEqual(m.actions[-1]['reason'], 'size_below_minimum')
        # 25 % daily halt: no new margin-sized entry once the day is down 25 %
        self.specs[ALT] = dict(ALT_SPEC)
        state = dict(odt.new_state(), paused=False)
        fake = self.fake(equity=1000); run(fake, paper(), state=state)
        fake = self.fake(equity=749); m, _ = run(fake, paper(**{ALT: alt_signal(sizing='margin')}), state=state)
        self.assertTrue(state['halted']); self.assertEqual(self.orders(fake), [])


# ------------------------------------------------------------------------------------------------------------ paper
COINS = ('A-USDT-SWAP', 'B-USDT-SWAP', 'C-USDT-SWAP', 'D-USDT-SWAP')


def flat(n, px=100.0):
    ts = [T0 + i * H for i in range(n)]
    return {k: X.Series(k, ts, [px] * n, [px * 1.001] * n, [px * .999] * n, [px] * n) for k in COINS}


class PaperMarginTests(unittest.TestCase):
    def setUp(self):
        self.entries = {}
        sig = mock.patch.object(X, 'signal_sets', side_effect=lambda g, series: (self.entries.get(int(g['lookback']), {}), {}))
        sig.start(); self.addCleanup(sig.stop)

    def seg(self, risk=.075, lev=10, lookback=11):
        return dict(id='s', genes=base_genes(lookback=lookback, risk=risk, leverage=lev), from_i=0, wind_i=None, boosts=[], tenure_ts=T0 - 100 * H)

    def acc(self, margin_from, n=40, **kw):
        self.entries = {11: {5: [('A-USDT-SWAP', 1, 5.0)], 20: [('B-USDT-SWAP', 1, 5.0)]}}
        return X.simulate_account([self.seg(**kw)], flat(n), 2, n, equity=10000.0, margin_from=margin_from)

    def test_fills_on_or_before_the_cutover_keep_stop_risk_after_use_margin(self):
        frozen = self.acc(n=40, margin_from=10)
        a, b = sorted(frozen['positions'], key=lambda p: p['opened'])
        self.assertAlmostEqual(a['qty'] * 5.0, 10000.0 * .075, delta=1)             # filled bar 6 ≤ 10: risk 7.5 % ÷ stop
        px = 100.0 * (1 + X.SLIP)
        mark = 10000.0 - a['qty'] * px * X.FEE + a['qty'] * (100.0 - a['entry'])
        self.assertAlmostEqual(b['qty'], mark * .06 * 10 / px, places=6)            # filled bar 21: m 6 % × 10× ÷ price
        self.assertAlmostEqual(b['risk'], .06)
        everything = self.acc(n=40, margin_from=None)                                 # None = margin on every fill
        self.assertAlmostEqual(min(everything['positions'], key=lambda p: p['opened'])['qty'], 10000.0 * .06 * 10 / px, places=6)
        with mock.patch.dict(X.ACCOUNT_RULES, {'sizing': 'stop_risk'}):               # the stop-risk engine switch
            old = self.acc(n=40, margin_from=None)
        self.assertEqual([p['qty'] for p in sorted(old['positions'], key=lambda p: p['opened'])][0], a['qty'])

    def test_existing_positions_are_never_resized_and_history_before_the_cutover_is_byte_identical(self):
        pending = self.acc(n=40, margin_from=39)          # paper with the key pending: the stop-risk rule on every bar
        stamped = self.acc(n=40, margin_from=10)
        a_old = next(p for p in pending['positions'] if p['inst'] == 'A-USDT-SWAP')
        a_new = next(p for p in stamped['positions'] if p['inst'] == 'A-USDT-SWAP')
        self.assertEqual(a_old, a_new)                                                # same qty, margin, stop… after the cutover too
        cut = T0 + 10 * H
        self.assertEqual([c for c in pending['curve'] if c[0] <= cut + H * 10], [c for c in stamped['curve'] if c[0] <= cut + H * 10])   # B fills at 21
        self.assertEqual(json.dumps([c for c in pending['curve'] if c[0] <= T0 + 20 * H]), json.dumps([c for c in stamped['curve'] if c[0] <= T0 + 20 * H]))
        self.assertNotEqual(next(p for p in pending['positions'] if p['inst'] == 'B-USDT-SWAP')['qty'],
                            next(p for p in stamped['positions'] if p['inst'] == 'B-USDT-SWAP')['qty'])

    def test_margin_sized_short_without_a_valid_target_keeps_no_target(self):
        # a short whose 3R target would be below 0: no target (PR #118), size by margin
        self.entries = {11: {5: [('A-USDT-SWAP', -1, 40.0)]}}
        r = X.simulate_account([self.seg(risk=.10, lev=4)], flat(30), 2, 30, equity=10000.0)
        p, px = r['positions'][0], 100.0 * (1 - X.SLIP)
        self.assertIsNone(p['target'])
        self.assertAlmostEqual(p['qty'], 10000.0 * .10 * 4 / px, places=6)
        self.assertAlmostEqual(p['risk'], .10)

    def test_simulate_fitness_sizes_by_margin(self):
        self.entries = {11: {5: [('A-USDT-SWAP', 1, 5.0)]}}
        g = base_genes(lookback=11, risk=.10, leverage=7)
        r = X.simulate(g, {'A-USDT-SWAP': flat(30)['A-USDT-SWAP']}, 2, 30, equity=10000.0)
        p, px = r['positions'][0], 100.0 * (1 + X.SLIP)
        self.assertAlmostEqual(p['qty'], 10000.0 * .10 * 7 / px, places=6)          # m 10 % × 7× ÷ price
        old = X.simulate(g, {'A-USDT-SWAP': flat(30)['A-USDT-SWAP']}, 2, 30, equity=10000.0, rules=dict(X.ACCOUNT_RULES, sizing='stop_risk'))
        self.assertAlmostEqual(old['positions'][0]['qty'], 10000.0 * .10 / 5.0, places=6)
        self.assertEqual(SZ.SIZING, 'margin')

    # --- paper.update: cutover key, signals marker, replay -----------------------------------------------------------
    def state(self):
        nodes = {'n1': dict(id='n1', genes=base_genes(lookback=11, risk=.075, leverage=10), species='momentum', fitness=1.0, qualified=True)}
        segs = [dict(id='a1', champion='n1', slot=0, genes=fix_genes(nodes['n1']['genes']), species='momentum', from_ts=T0 - 100 * H, wind_ts=None,
                     boosts=[], reason='carry')]
        acc = dict(from_ts=T0, start_equity=10000.0, next=1, segs=segs, side_cap_from=T0, risk_rules_from=T0, risk_cap_from=T0,
                   exit_rules_from=T0, trail_risk_from=T0, price_off_live_from=T0, manual_from={})
        return dict(nodes=nodes, team=[dict(id='n1', since=T0)], leader=None, champion=None, paper=dict(account=acc, start_equity=10000.0))

    def update(self, state, n, **kw):
        cap, orig = {}, X.simulate_account
        with mock.patch.object(P, 'entry_gate', lambda node, state=None, streak=None: dict(passed=True, reasons=[])), \
                mock.patch.object(P, 'MANUAL_CLOSES', {}), \
                mock.patch.object(P, 'simulate_account', side_effect=lambda *a, **k: cap.setdefault('r', orig(*a, **k))):
            view, signals = P.update(state, flat(n), [], **kw)
        return view, signals, cap['r']

    def test_cutover_key_is_stamped_once_on_the_next_hour_and_marks_signals_after_it(self):
        self.assertIn('margin_sizing_from', P.CUTOVER_KEYS)
        L = 20
        self.entries = {11: {10: [('A-USDT-SWAP', 1, 5.0)], L + 1: [('C-USDT-SWAP', 1, 5.0)], L + 2: [('D-USDT-SWAP', 1, 5.0)]}}
        st = self.state()
        now = T0 + L * H + 2 * H - 3 * 60_000                                         # GitHub run ~57 min late → C = ts[L] + 2 h
        C = P.effective_bar(now)
        view, signals, res = self.update(st, L + 1, now=now)
        acc = st['paper']['account']
        self.assertEqual(acc['margin_sizing_from'], C)
        self.assertEqual(C, T0 + (L + 2) * H)
        self.assertIn('margin_sizing_from', [x.get('key') for x in view['scheduled']])  # pending: bar C not in the data yet
        self.assertEqual({k: s.get('sizing') for k, s in signals.items()}, {'A-USDT-SWAP': None})   # A filled before the stamp
        # a later run: never moved; C fills at bar L+2 (open C, old rule), D at L+3 (after C, margin) — and marked
        view, signals, res = self.update(st, L + 4, now=now + 2 * H)
        self.assertEqual(acc['margin_sizing_from'], C)
        by = {p['inst']: p for p in res['positions']}
        self.assertAlmostEqual(by['A-USDT-SWAP']['risk'], .075); self.assertAlmostEqual(by['C-USDT-SWAP']['risk'], .075)   # stop risk
        self.assertAlmostEqual(by['D-USDT-SWAP']['risk'], .06)                                                              # margin m
        self.assertEqual({k: s.get('sizing') for k, s in signals.items()},
                         {'A-USDT-SWAP': None, 'C-USDT-SWAP': None, 'D-USDT-SWAP': 'margin'})

    def test_replay_with_the_key_missing_is_the_old_rule_and_matches_github_once_stamped(self):
        L = 20
        self.entries = {11: {10: [('A-USDT-SWAP', 1, 5.0)], L + 2: [('B-USDT-SWAP', 1, 5.0)]}}
        unstamped = self.state()
        self.update(unstamped, L + 1, now=T0 + (L + 1) * H + 10 * 60_000)
        del unstamped['paper']['account']['margin_sizing_from']                        # = a state written before this code
        self.assertNotIn('margin_sizing_from', unstamped['paper']['account'])
        old = json.loads(json.dumps(unstamped))
        _, sig_r, res_r = self.update(old, L + 4, replay=True)                        # hourly server: key pending → old rule
        self.assertEqual(old, json.loads(json.dumps(unstamped)))
        self.assertFalse(any('sizing' in s for s in sig_r.values()))
        self.assertAlmostEqual(next(p for p in res_r['positions'] if p['inst'] == 'B-USDT-SWAP')['risk'], .075)
        # GitHub stamps it (C = ts[L+2]), the server replays the stamped state: identical to GitHub on the same bars
        stamped = json.loads(json.dumps(unstamped))
        self.update(stamped, L + 1, now=T0 + (L + 1) * H + 10 * 60_000)                 # the first GitHub run with this code
        self.assertEqual(stamped['paper']['account']['margin_sizing_from'], T0 + (L + 2) * H)
        _, sig_s, res_s = self.update(json.loads(json.dumps(stamped)), L + 4, replay=True)
        _, sig_g, res_g = self.update(stamped, L + 4, now=T0 + (L + 4) * H + 10 * 60_000)  # the next GitHub run
        self.assertEqual(sig_s, sig_g); self.assertEqual(res_s['positions'], res_g['positions'])
        # B fills at bar L+3 (after C): margin, and marked; A (bar 11) unchanged in every replay
        b = next(p for p in res_g['positions'] if p['inst'] == 'B-USDT-SWAP'); self.assertAlmostEqual(b['risk'], .06)
        self.assertEqual(sig_g['B-USDT-SWAP'].get('sizing'), 'margin')
        self.assertEqual(next(p for p in res_g['positions'] if p['inst'] == 'A-USDT-SWAP'),
                         next(p for p in res_r['positions'] if p['inst'] == 'A-USDT-SWAP'))


if __name__ == '__main__':
    unittest.main()
