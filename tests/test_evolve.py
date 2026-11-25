import json
import math
import random
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'evolve'))
import execution as X   # noqa: E402
import genome as G      # noqa: E402
import evolution as E   # noqa: E402
import paper as P       # noqa: E402
import data as D        # noqa: E402
import run as R         # noqa: E402

H = 3600_000
T0 = 1_760_000_000_000 // H * H
# These tests pin the stop-risk sizing engine (risk ÷ stop distance), the rule in force before MARGIN SIZING (2026-11-25,
# sizing.py) and still used for fills on or before account['margin_sizing_from']; tests/test_margin_sizing.py covers margin.
STOP_RISK = mock.patch.dict(X.ACCOUNT_RULES, {'sizing': 'stop_risk'})


def series_from(closes, inst='BTC-USDT-SWAP', spread=.004, opens=None):
    o = opens or [closes[0]] + closes[:-1]
    h = [max(a, b) * (1 + spread) for a, b in zip(o, closes)]
    l = [min(a, b) * (1 - spread) for a, b in zip(o, closes)]
    return X.Series(inst, [T0 + i * H for i in range(len(closes))], list(o), h, l, list(closes))


def walk(n, seed, drift=0.0):
    rng, x, out = random.Random(seed), 100.0, []
    for _ in range(n):
        x *= math.exp(drift + rng.gauss(0, .01)); out.append(x)
    return out


def base_genes(**kw):
    g = G.random_genome(random.Random(1), 'momentum')
    g.update(direction='both', lookback=20, stop_atr=2.0, target_rr=0.0, max_hold=240, risk=.02, leverage=10, vol_min=.3, vol_max=4.0, tf=1, trail=0)
    g.update(kw)
    return g


# The trail / risk rules of the era before account['trail_risk_from'] (execution TRAIL & RISK RULES): tests of other
# mechanics (sizing eras, liquidation order) run under them so a same-bar share or a new-seat half does not interfere.
PRIOR_ERA = dict(trail=X.PRIOR_TRAIL, **X.PRIOR_RISK)


class ExecutionTests(unittest.TestCase):
    def test_precomputed_entries_match_bar_by_bar_signals_for_every_species_and_timeframe(self):
        rnd = random.Random(7)
        btc = series_from(walk(900, 1), 'BTC-USDT-SWAP'); eth = series_from(walk(900, 2, .0003), 'ETH-USDT-SWAP')
        for s in (btc, eth):
            s.v = [rnd.uniform(1, 10) * (8 if rnd.random() < .03 else 1) for _ in range(s.n)]
        eth.ref = btc
        series = {'BTC-USDT-SWAP': btc, 'ETH-USDT-SWAP': eth}
        checked = 0
        for k in range(60):
            g = G.random_genome(random.Random(k), G.SPECIES[k % len(G.SPECIES)])
            g['vol_min'], g['vol_max'] = min(g['vol_min'], .6), max(g['vol_max'], 2.5)
            want = {}
            for i in range(btc.n):
                for inst, s in series.items():
                    d = X.signal(g, s, i)
                    if d:
                        want.setdefault(i, []).append((inst, d, X.stop_distance(g, s, i)))
            self.assertEqual(X.entries(g, series), want, g)
            checked += len(want)
        self.assertGreater(checked, 50)

    def test_signal_on_close_fills_next_open_with_slippage_and_fee(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 5          # breakout on bar 40
        s = series_from(closes)
        r = X.simulate(base_genes(), {'BTC-USDT-SWAP': s}, 30, len(closes))
        self.assertEqual(len(r['positions']), 1)
        p = r['positions'][0]
        self.assertEqual(p['opened'], s.ts[41])                   # filled on the NEXT bar
        self.assertAlmostEqual(p['entry'], s.o[41] * (1 + X.SLIP))
        self.assertLess(r['equity'], X.START_EQUITY + p['qty'] * (s.c[-1] - p['entry']) + 1e-6)   # entry fee paid

    @STOP_RISK
    def test_stop_first_when_a_bar_touches_both_and_gaps_fill_at_open(self):
        closes = [100.0] * 40 + [110.0, 110.0]
        s = series_from(closes)
        g = base_genes(target_rr=1.0)
        r = X.simulate(g, {'BTC-USDT-SWAP': s}, 30, len(closes))
        p = r['positions'][0]
        # next bar spans far beyond stop and target: the stop is assumed first
        s.o.append(p['entry']); s.h.append(p['target'] * 1.01); s.l.append(p['stop'] * .99); s.c.append(p['entry']); s.ts.append(s.ts[-1] + H); s.n += 1
        s._cache.clear()
        r2 = X.simulate(g, {'BTC-USDT-SWAP': s}, 30, s.n)
        self.assertEqual(r2['trades'][-1]['reason'], 'stop')
        self.assertAlmostEqual(r2['trades'][-1]['exit'], p['stop'] * (1 - X.SLIP))
        below = p['stop'] * .995                                                          # opens below the stop, above liquidation
        self.assertGreater(below, p['liq'])
        gap = series_from(closes + [below], opens=[closes[0]] + closes[:-1] + [below])
        r3 = X.simulate(g, {'BTC-USDT-SWAP': gap}, 30, gap.n)
        self.assertEqual(r3['trades'][-1]['reason'], 'stop'); self.assertAlmostEqual(r3['trades'][-1]['exit'], below * (1 - X.SLIP))
        # Cross margin: a −27 % gap through the stop (and through the old isolated liq estimate) is still a stop at the
        # open, because this 2 % risk position leaves account equity far above total maintenance margin.
        self.assertLess(80.0, p['liq'])
        crash = series_from(closes + [80.0], opens=[closes[0]] + closes[:-1] + [80.0])
        r4 = X.simulate(g, {'BTC-USDT-SWAP': crash}, 30, crash.n)
        t = r4['trades'][-1]
        self.assertEqual(t['reason'], 'stop'); self.assertAlmostEqual(t['exit'], 80.0 * (1 - X.SLIP))
        self.assertGreater(r4['equity'], 0)
        # A big position (10 % risk on a tight stop; a 100× gene is capped at 20×): the same kind of gap takes account equity to or below
        # maintenance margin, so the whole book is liquidated at the bar's open, with the close fee and no slippage.
        big = base_genes(target_rr=1.0, risk=.10, leverage=100, stop_atr=.5)
        rb = X.simulate(big, {'BTC-USDT-SWAP': s}, 30, len(closes))
        pb, cash = rb['positions'][0], rb['cash']
        px = 103.0
        self.assertTrue(X.book_liquidated(cash, {'BTC-USDT-SWAP': dict(pb, mmr=X.MMR_MAJOR)}, {'BTC-USDT-SWAP': px}))
        gap = series_from(closes + [px], opens=[closes[0]] + closes[:-1] + [px])
        r5 = X.simulate(big, {'BTC-USDT-SWAP': gap}, 30, gap.n)
        t = r5['trades'][-1]
        self.assertEqual(t['reason'], 'liquidation'); self.assertEqual(t['exit'], px); self.assertEqual(r5['positions'], [])
        close_fee, entry_fee = pb['qty'] * px * X.FEE, pb['qty'] * pb['entry'] * X.FEE
        self.assertAlmostEqual(t['pnl'], pb['qty'] * (px - pb['entry']) - close_fee - entry_fee, places=3)
        self.assertAlmostEqual(r5['cash'], cash + pb['qty'] * (px - pb['entry']) - close_fee, places=3)
        # past the bankruptcy price the account is floored at zero (no negative balance)
        wipe = series_from(closes + [90.0], opens=[closes[0]] + closes[:-1] + [90.0])
        r6 = X.simulate(big, {'BTC-USDT-SWAP': wipe}, 30, wipe.n)
        self.assertEqual(r6['trades'][-1]['reason'], 'liquidation'); self.assertEqual(r6['equity'], 0.0)

    def test_contract_mode_lots_leverage_margin_and_funding(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 30
        s = series_from(closes)
        g = base_genes(risk=.05)
        free = X.simulate(g, {'BTC-USDT-SWAP': s}, 30, len(closes))['positions'][0]
        s2 = series_from(closes); s2.spec = (0.1, 1.0, 1.0)                     # 0.1 coin per contract, whole contracts
        p = X.simulate(g, {'BTC-USDT-SWAP': s2}, 30, len(closes))['positions'][0]
        self.assertAlmostEqual(p['qty'] / .1, round(p['qty'] / .1)); self.assertLessEqual(p['qty'], free['qty'])
        dist = (p['entry'] - p['stop']) / p['entry']
        self.assertEqual(p['lever'], X.clamp_leverage(g.get('leverage', X.DEFAULT_LEVERAGE), dist, cap=X.LEVERAGE))
        self.assertGreater(p['stop'], p['liq'])   # long: stop sits before liquidation
        self.assertAlmostEqual(p['margin'], p['qty'] * p['entry'] / p['lever'], places=3)
        self.assertAlmostEqual(p['liq'], X.liquidation_price(p['entry'], 1, p['lever']))
        s3 = series_from(closes); s3.spec = (1000.0, 1.0, 1.0)                  # one contract is bigger than the risk allows: no trade
        self.assertEqual(X.simulate(g, {'BTC-USDT-SWAP': s3}, 30, len(closes))['positions'], [])
        s4 = series_from(closes); s4.funding = {t: .001 for t in s4.ts if t % (8 * H) == 0}   # longs pay 0.1 % every 8 h
        r4 = X.simulate(g, {'BTC-USDT-SWAP': s4}, 30, len(closes))
        p4 = r4['positions'][0]
        paid = sum(free['qty'] * s4.o[i] * .001 for i in range(len(closes)) if s4.ts[i] in s4.funding and s4.ts[i] > p4['opened'])
        self.assertGreater(paid, 0); self.assertAlmostEqual(p4['funding'], round(paid, 4), places=3)
        r1 = X.simulate(g, {'BTC-USDT-SWAP': series_from(closes)}, 30, len(closes))
        self.assertAlmostEqual(r1['equity'] - r4['equity'], paid, places=6)

    def test_funding_map_defaults_before_history(self):
        ts = [T0 + i * H for i in range(48)]
        eight = [t for t in ts if t % (8 * H) == 0]
        rows = [[eight[-1], -.0003]]
        m = D.funding_map(rows, ts)
        self.assertEqual(m[eight[-1]], -.0003)
        self.assertEqual({t: v for t, v in m.items() if t != eight[-1]}, {t: D.FUNDING_DEFAULT for t in eight[:-1]})

    def test_no_time_exit_any_more_and_metrics(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 300            # held far past max_hold (4 bars) and MAX_HOLD_H
        s = series_from(closes)
        r = X.simulate(base_genes(max_hold=4), {'BTC-USDT-SWAP': s}, 30, s.n)
        self.assertEqual(r['trades'], [])
        self.assertEqual(len(r['positions']), 1)
        self.assertEqual(r['metrics']['trades'], len(r['trades']))
        self.assertGreaterEqual(r['metrics']['max_dd'], 0)
        # the paper replay of bars on or before exit_from still has the old time exit, later bars do not
        seg = [dict(id='m', genes=base_genes(max_hold=4), from_i=0)]
        old = X.simulate_account(seg, {'BTC-USDT-SWAP': s}, 30, s.n, exit_from=s.n)
        self.assertEqual([t['reason'] for t in old['trades']], ['time'])
        self.assertEqual(old['trades'][0]['closed'], s.ts[41 + 4])
        new = X.simulate_account(seg, {'BTC-USDT-SWAP': s}, 30, s.n)
        self.assertEqual((new['trades'], len(new['positions'])), ([], 1))

    def test_no_look_ahead(self):
        closes = walk(600, 3)
        s1, s2 = series_from(closes), series_from(closes[:400] + [c * 1.5 for c in closes[400:]])
        g = base_genes()
        a = X.simulate(g, {'BTC-USDT-SWAP': s1}, 50, 400)
        b = X.simulate(g, {'BTC-USDT-SWAP': s2}, 50, 400)
        self.assertEqual(a['trades'], b['trades']); self.assertEqual(a['equity'], b['equity'])

    def test_paper_replay_uses_the_same_engine_as_the_backtest(self):
        sr = {'BTC-USDT-SWAP': series_from(walk(900, 5)), 'ETH-USDT-SWAP': series_from(walk(900, 6), 'ETH-USDT-SWAP')}
        g = base_genes()
        seg = dict(from_ts=sr['BTC-USDT-SWAP'].ts[500], start_equity=10000.0)
        paper = P.run_segment(seg, g, sr)
        back = X.simulate(g, sr, 500, 900, equity=10000.0, curve_every=1)
        self.assertEqual(paper['trades'], back['trades']); self.assertEqual(paper['equity'], back['equity'])
        self.assertEqual(paper['positions'], back['positions'])


class MergedGeneTests(unittest.TestCase):
    """Genes merged from the rolling-evolution system: timeframe, trailing stop, volume surge, relative strength."""

    def test_old_genomes_get_neutral_defaults_and_trade_exactly_as_before(self):
        g = base_genes()
        old = {k: v for k, v in g.items() if k not in G.DEFAULTS}
        self.assertEqual(G.fix(dict(old))['tf'], 1); self.assertEqual(G.fix(dict(old))['trail'], 0)
        self.assertEqual(G.fix(dict(old))['leverage'], 10)
        self.assertIn('1 小时', G.describe(old))                   # dashboard text for genomes stored before the new genes
        self.assertIn('杠杆 10×', G.describe(old))
        self.assertNotIn('tf', old)
        s = series_from(walk(600, 4))
        a = X.simulate(G.fix(dict(old)), {'BTC-USDT-SWAP': s}, 50, 600)
        b = X.simulate(g, {'BTC-USDT-SWAP': s}, 50, 600)
        self.assertEqual(a['trades'], b['trades'])

    def test_population_saved_before_the_new_genes_keeps_evolving(self):
        sr = {'BTC-USDT-SWAP': series_from(walk(700, 11)), 'ETH-USDT-SWAP': series_from(walk(700, 12), 'ETH-USDT-SWAP')}
        sr['ETH-USDT-SWAP'].ref = sr['BTC-USDT-SWAP']
        sp = (60, 480, 700)
        st = E.init_state(5, sr, sp)
        for n in st['nodes'].values():                    # as stored on paper-data before this change
            for k in G.DEFAULTS: n['genes'].pop(k, None)
        row = E.generation(st, sr, sp, D.observe_volatility(sr))
        self.assertEqual(len(st['population']), E.POPULATION)
        self.assertTrue(all('tf' in st['nodes'][i]['genes'] for i in st['population'] if st['nodes'][i]['born'] == row['gen']))

    def test_a_strategy_holds_at_most_three_positions(self):
        insts = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE']
        closes = [100.0] * 40 + [110.0] + [111.0] * 5                      # every coin breaks out on the same bar
        sr = {f'{c}-USDT-SWAP': series_from(closes, f'{c}-USDT-SWAP') for c in insts}
        r = X.simulate(base_genes(), sr, 30, len(closes))
        self.assertEqual(len(r['positions']), X.MAX_POSITIONS)
        self.assertEqual([p['inst'] for p in r['positions']], [f'{c}-USDT-SWAP' for c in insts[:3]])

    def test_4h_bars_are_complete_utc_aligned_and_decide_only_when_they_close(self):
        s = series_from(walk(100, 2))
        bars, closes_at, last = s.tf(4)
        for j in range(bars.n):
            self.assertEqual(bars.ts[j] % (4 * H), 0)
            k = s.ts.index(bars.ts[j])
            self.assertEqual(bars.o[j], s.o[k]); self.assertEqual(bars.c[j], s.c[k + 3])
            self.assertEqual(bars.h[j], max(s.h[k:k + 4])); self.assertEqual(closes_at[k + 3], j)
        self.assertTrue(all(c == -1 for i, c in enumerate(closes_at) if (s.ts[i] + H) % (4 * H)))
        self.assertTrue(all(last[i] <= i // 4 for i in range(s.n)))
        g = base_genes(tf=4)
        r = X.simulate(g, {'BTC-USDT-SWAP': s}, 30, s.n)
        for t in r['trades'] + r['positions']:
            self.assertEqual(t['opened'] % (4 * H), 0)               # decided on a 4H close, filled at the next 1H open

    def test_trailing_stop_follows_the_best_price_and_never_loosens(self):
        closes = [100.0] * 40 + [110.0] + [112.0, 113.5, 115.0, 114.0]     # +5 after the fill: past 1R, short of the 3R target
        s = series_from(closes)
        dist = X.stop_distance(base_genes(), s, 40)
        a = X.simulate(base_genes(), {'BTC-USDT-SWAP': s}, 30, s.n)['positions'][0]
        b = X.simulate(base_genes(trail=1), {'BTC-USDT-SWAP': s}, 30, s.n)['positions'][0]
        self.assertEqual(a, b)                                       # the trail gene no longer changes anything
        best = max(s.h[41:])
        self.assertGreaterEqual(best - a['entry'], X.TRAIL_START_R * dist)   # moved ≥ 1R: the trail is on
        self.assertTrue(a['trailing'])
        self.assertAlmostEqual(a['stop'], best - X.TRAIL_DIST_R * dist)
        self.assertAlmostEqual(a['best'], best)

    def test_volume_surge_and_relative_strength_signals(self):
        closes = [100.0] * 60 + [101.0]
        vol = [10.0] * 60 + [100.0]
        s = series_from(closes); s.v = vol
        g = base_genes(species='vol_surge', lookback=20, vol_mult=3.0)
        self.assertEqual(X.signal(g, s, 60), 1)
        self.assertEqual(X.signal(g, s, 59), 0)
        btc = series_from([100.0] * 70)
        alt = series_from([100.0] * 60 + [100.0 * 1.01 ** k for k in range(1, 11)], 'ETH-USDT-SWAP'); alt.ref = btc
        g = base_genes(species='rel_strength', lookback=10, rs_th=.05)
        fired = [i for i in range(20, 70) if X.signal(g, alt, i)]
        self.assertEqual(len(fired), 1); self.assertEqual(X.signal(g, alt, fired[0]), 1)
        self.assertEqual([i for i in range(20, 70) if X.signal(g, btc, i)], [])   # BTC has no reference: never fires


class RobustFitnessAndMigrantTests(unittest.TestCase):
    def setUp(self):
        self.sr = {'BTC-USDT-SWAP': series_from(walk(900, 21)), 'ETH-USDT-SWAP': series_from(walk(900, 22), 'ETH-USDT-SWAP')}
        self.sr['ETH-USDT-SWAP'].ref = self.sr['BTC-USDT-SWAP']
        self.split = (60, 630, 900)

    def test_training_is_scored_in_segments_and_steady_beats_lumpy(self):
        r = E.evaluate(base_genes(), self.sr, self.split)
        self.assertEqual(len(r['train']['folds']), E.FOLDS)
        self.assertEqual(sum(f['trades'] for f in r['train']['folds']), r['train']['trades'])
        steady = [dict(score=2.0, ret=5.0, max_dd=5, trades=10)] * 4
        lumpy = [dict(score=-1.0, ret=-2.0, max_dd=5, trades=10)] * 3 + [dict(score=8.0, ret=40.0, max_dd=5, trades=10)]
        self.assertGreater(E.robust_fitness(steady, 40), E.robust_fitness(lumpy, 40))
        self.assertLess(E.robust_fitness(steady, 15), E.robust_fitness(steady, 40))      # too few trades are damped

    def test_migrants_join_once_replacing_the_weakest_non_team_members(self):
        st = E.init_state(7, self.sr, self.split)
        mig = json.loads((Path(__file__).resolve().parents[1] / 'tools' / 'evolve' / 'migrants.json').read_text())
        before = len(st['population'])
        added = E.add_migrants(st, mig, self.sr, self.split)
        self.assertEqual(len(added), len(mig['strategies'])); self.assertEqual(len(st['population']), before)
        self.assertTrue(all(st['nodes'][i]['how'] == 'migrant' and st['nodes'][i]['origin'] for i in added))
        self.assertEqual(E.add_migrants(st, mig, self.sr, self.split), [])                # only once per version
        for m in mig['strategies']:
            g = G.fix(dict(m['genes']))   # old migrant files may omit newer genes (leverage, …)
            for k, (kind, lo, hi) in G.GENES.items():
                if kind != 'choice': self.assertTrue(lo <= g[k] <= hi, (k, g[k]))
                else: self.assertIn(g[k], lo)
            self.assertEqual(g['leverage'], m['genes'].get('leverage', 10) if 1 <= m['genes'].get('leverage', 10) <= 20 else g['leverage'])


class GenomeTests(unittest.TestCase):
    def test_bounds_and_relations_hold_after_mutation_and_crossover(self):
        rng = random.Random(7)
        for _ in range(300):
            a, b = G.random_genome(rng), G.random_genome(rng)
            for g in (G.mutate(a, rng, rate=1.0, scale=1.0), G.crossover(a, b, rng)):
                for k, (kind, lo, hi) in G.GENES.items():
                    if kind == 'choice': self.assertIn(g[k], lo)
                    else: self.assertTrue(lo <= g[k] <= hi, (k, g[k]))
                self.assertGreater(g['slow'], g['fast']); self.assertGreater(g['vol_max'], g['vol_min'])
                self.assertTrue(g['target_rr'] == 0 or g['target_rr'] >= .5)
        self.assertIn('动量突破', G.describe(G.random_genome(random.Random(1), 'momentum')))


def tiny_market(n=900):
    return {i: series_from(walk(n, k, drift=.0003 * (k % 3 - 1)), i) for k, i in enumerate(D.INSTRUMENTS[:3])}


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.sr = tiny_market(); self.split = D.split(self.sr)
        self.obs = D.observe_volatility(self.sr)
        for k, v in (('TEAM_SIZE', 5), ('TEAM_DIRECTION_CAP', 3)):         # the seat rules are tested on a team of five
            self.addCleanup(setattr, E, k, getattr(E, k)); setattr(E, k, v)

    def test_team_has_eight_seats_and_members_that_stop_qualifying_are_replaced(self):
        E.TEAM_SIZE, E.TEAM_DIRECTION_CAP = 8, 5
        st, ids = self._setup([(12 - k, True, 3, 10) for k in range(10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % len(G.SPECIES)]
        E.update_hall(st); E.choose_team(st, T0)
        self.assertEqual([m['id'] for m in st['team']], ids[:8])
        h = 3600_000
        self.assertEqual(E.choose_team(st, T0 + h), [])    # still-qualified members keep the seat, even before tenure
        st['nodes'][ids[3]]['qualified'] = False; E.update_hall(st)
        changes = E.choose_team(st, T0 + 2 * h)            # first run after failing OOS: replaced, tenure does not protect it
        self.assertEqual([(c[1], c[3]) for c in changes], [(ids[3], 'entry_gate')]); self.assertIn(changes[0][2], ids[8:])
        self.assertNotIn(ids[3], [m['id'] for m in st['team']])
        st['nodes'][ids[5]]['qualified'] = False; E.update_hall(st)
        E.choose_team(st, T0 + 3 * h)
        self.assertNotIn(ids[5], [m['id'] for m in st['team']])
        st['nodes'][ids[5]]['qualified'] = True; E.update_hall(st)
        E.choose_team(st, T0 + 4 * h)
        self.assertNotIn(ids[5], st.get('unqualified', {}))                  # no longer seated, so the fail count is cleared

    def test_generation_keeps_size_carries_elites_and_records_lineage(self):
        st = E.init_state(1, self.sr, self.split)
        self.assertEqual(len(st['population']), E.POPULATION)
        pop = [st['nodes'][i] for i in st['population']]
        best = max(pop, key=lambda n: n['fitness'])
        species_best = {sp: max((n for n in pop if n['species'] == sp), key=lambda n: n['fitness']) for sp in G.SPECIES}
        row = E.generation(st, self.sr, self.split, self.obs)
        self.assertEqual(len(st['population']), E.POPULATION)
        self.assertIn(best['id'], st['population'])                              # the overall best always survives
        for n in species_best.values(): self.assertIn(n['id'], st['population'])  # and the best of every species
        kids = [st['nodes'][i] for i in st['population'] if st['nodes'][i]['born'] == 1]
        self.assertEqual(row['born'], len(kids)); self.assertEqual(row['killed'], len(kids))
        self.assertTrue(any(k['how'] == 'crossover' and len(k['parents']) == 2 for k in kids))
        self.assertEqual(sum(1 for k in kids if k['how'] == 'immigrant'), E.IMMIGRANTS)
        for sp in G.SPECIES: self.assertGreaterEqual(row['species'][sp], E.MIN_PER_SPECIES)

    def test_collapsed_population_is_repaired_and_crowded_species_are_discounted(self):
        st = E.init_state(4, self.sr, self.split)
        nodes = st['nodes']
        for i, nid in enumerate(st['population']):                               # 37 'trend', 1 each of the others
            sp = G.SPECIES[i] if i < 3 and G.SPECIES[i] != 'trend' else 'trend'
            nodes[nid]['genes']['species'] = nodes[nid]['species'] = sp
        nodes[st['population'][0]]['species'] = 'momentum'
        row = E.generation(st, self.sr, self.split, self.obs)
        for sp in G.SPECIES: self.assertGreaterEqual(row['species'][sp], E.MIN_PER_SPECIES, sp)
        # The floor itself, directly: children that are all 'trend' get topped up with niche members of the others.
        rng = random.Random(9); pop = [st['nodes'][i] for i in st['population']]
        elites = [n for n in pop if n['species'] == 'trend'][:2]
        kids = [E.new_node(st, G.random_genome(rng, 'trend'), [], 99, 'mutation', self.sr, self.split) for _ in range(20)]
        fixed = E.protect_species(st, elites, kids, pop, 99, rng, self.obs, self.sr, self.split)
        self.assertEqual(len(fixed), 20)
        for sp in G.SPECIES: self.assertGreaterEqual(sum(1 for n in elites + fixed if n['species'] == sp), E.MIN_PER_SPECIES, sp)
        self.assertEqual(sum(1 for n in fixed if n['how'] == 'niche'), (len(G.SPECIES) - 1) * E.MIN_PER_SPECIES)
        self.assertEqual(len(st['population']), E.POPULATION)
        counts = {'trend': 36, 'momentum': 1}
        a, b = dict(species='trend', fitness=4.0), dict(species='momentum', fitness=1.0)
        self.assertGreater(E.shared_fitness(b, counts), E.shared_fitness(a, counts))   # 4/6 < 1/1
        self.assertLess(E.shared_fitness(dict(species='trend', fitness=-1.0), counts), -1.0)

    def _setup(self, fits):
        """Population where the first len(fits) strategies have the given (fitness, qualified, test_ret, test_dd)."""
        st = E.init_state(2, self.sr, self.split)
        nodes, ids = st['nodes'], st['population'][:len(fits)]
        for nid, (fit, ok, ret, dd) in zip(ids, fits):
            nodes[nid].update(fitness=fit, qualified=ok, test=dict(nodes[nid]['test'], ret=ret, max_dd=dd), final=dict(nodes[nid]['test'], ret=ret, max_dd=dd))
        for nid in st['population'][len(fits):]:
            nodes[nid].update(fitness=0.0, qualified=False)
        for nid in st['population']:
            nodes[nid]['genes']['direction'] = 'both'           # direction quota is tested on its own
        st['hall'] = []; E.update_hall(st)
        for nid, rec in zip(ids, fits):
            st['nodes'][nid]['oos_dd'] = rec[3]
        return st, ids

    def _grant_entry(self, st, nid, streak=E.ENTRY_STREAK):
        """Mark one individual as having passed the three-part paper entry gate (5-gen defense + OOS + DD)."""
        n = st['nodes'][nid]
        n['qualified'] = True
        n.setdefault('oos_dd', min(n.get('test', {}).get('max_dd', 10), 20))
        if n.get('oos_dd', 0) > E.ENTRY_MAX_DD:
            n['oos_dd'] = 20
        st['defense_id'] = nid
        st['defense_streak'] = streak
        st.setdefault('defense_streaks', {})[nid] = streak

    def test_team_fills_with_the_best_qualified_and_caps_each_species(self):
        st, ids = self._setup([(5.0, True, 3, 10), (4.0, True, 3, 10), (3.0, True, 3, 10), (2.0, True, 3, 10), (1.5, True, 3, 10), (1.0, True, 3, 10), (9.0, False, -1, 10)])
        nodes = st['nodes']
        for nid in ids: nodes[nid]['species'] = 'trend'
        nodes[ids[3]]['species'] = 'momentum'; nodes[ids[5]]['species'] = 'mean_revert'
        E.update_hall(st)
        changes = E.choose_team(st, T0)
        team = [m['id'] for m in st['team']]
        self.assertEqual(team[:4], [ids[0], ids[1], ids[3], ids[5]])       # cap first: 2 trend, then the other species; 9.0 (unqualified) never joins
        self.assertEqual(team[4], ids[2])                                  # the 5th seat then goes to the next best (cap gives way)
        self.assertEqual([c[3] for c in changes], ['first'] * 5)
        self.assertEqual(st['champion'], ids[0])
        self.assertEqual(E.choose_team(st, T0 + 3600_000), [])             # nothing changes on the next run
        # with only one species qualified, the cap gives way rather than leaving seats empty
        st2, ids2 = self._setup([(5.0 - k * .5, True, 3, 10) for k in range(6)])
        for nid in ids2: st2['nodes'][nid]['species'] = 'trend'
        E.update_hall(st2); E.choose_team(st2, T0)
        self.assertEqual([m['id'] for m in st2['team']], ids2[:5])

    def test_tenure_streak_and_one_swap_per_run_against_the_weakest(self):
        st, ids = self._setup([(5.0, True, 3, 10), (4.0, True, 3, 10), (3.0, True, 3, 10), (2.0, True, 3, 10), (1.0, True, 3, 10), (6.0, False, 3, 10), (7.0, False, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % 4]
        E.update_hall(st); E.choose_team(st, T0)
        self.assertEqual(len(st['team']), 5)
        h = 3600_000
        for nid in ids[5:]: st['nodes'][nid]['qualified'] = True
        E.update_hall(st)
        for k in range(1, 4):                                              # streaks build up, but tenure has not passed
            self.assertEqual(E.choose_team(st, T0 + k * h), [])
        self.assertEqual(E.choose_team(st, T0 + 25 * h), [])               # a day is not enough any more: tenure is a week
        W = E.MIN_TENURE // h
        changes = E.choose_team(st, T0 + (W + 1) * h)                      # one swap only, against the weakest (1.0)
        self.assertEqual(len(changes), 1); self.assertEqual(changes[0][1], ids[4]); self.assertEqual(changes[0][3], 'challenger')
        self.assertIn(changes[0][2], ids[5:])
        changes = E.choose_team(st, T0 + (W + 2) * h)                      # next run: the next-weakest seasoned member (2.0)
        self.assertEqual(len(changes), 1); self.assertEqual(changes[0][1], ids[3])

    def test_direction_quota_keeps_seats_for_the_other_direction_and_rebalances_after_tenure(self):
        st, ids = self._setup([(9 - k, True, 3, 10) for k in range(7)])
        nodes, h = st['nodes'], 3600_000
        for k, nid in enumerate(ids): nodes[nid]['species'] = G.SPECIES[k % 4]
        for nid in ids[:6]: nodes[nid]['genes']['direction'] = 'short'
        nodes[ids[6]]['genes']['direction'] = 'long'                          # the weakest qualified strategy is long-only
        E.update_hall(st); E.choose_team(st, T0)
        team = [m['id'] for m in st['team']]
        self.assertIn(ids[6], team)                                           # the quota seats the long strategy (weakest of all)
        self.assertEqual(team[:3], ids[:3])                                   # 3 shorts, then the long, then the 5th seat
        self.assertEqual(sum(nodes[i]['genes']['direction'] == 'short' for i in team), 4)   # goes to a short rather than stay empty
        # A team over the quota (e.g. from before the quota existed) is rebalanced once tenure has passed.
        st['team'] = [dict(id=i, since=T0) for i in ids[:5]]
        self.assertEqual(E.choose_team(st, T0 + h), [])                       # tenure first
        W = E.MIN_TENURE // h
        changes = E.choose_team(st, T0 + (W + 1) * h)
        self.assertEqual(len(changes), 1); self.assertEqual(changes[0][3], 'balance')
        self.assertEqual(changes[0][1], ids[4]); self.assertEqual(changes[0][2], ids[6])   # weakest short → the long
        self.assertEqual(E.choose_team(st, T0 + (W + 2) * h), [])             # no other long / both qualified: stays at 4 shorts
        nodes[st['population'][7]].update(fitness=.5, qualified=True, species='trend'); nodes[st['population'][7]]['genes']['direction'] = 'both'
        E.update_hall(st)
        changes = E.choose_team(st, T0 + (2 * W + 3) * h)                     # a 'both' strategy qualifies: one more seat moves
        self.assertEqual([(c[2], c[3]) for c in changes], [(st['population'][7], 'balance')])
        self.assertEqual(sum(nodes[m['id']]['genes']['direction'] == 'short' for m in st['team']), 3)
        self.assertEqual(E.choose_team(st, T0 + (2 * W + 4) * h), [])
        # The hall keeps the best qualified of each direction even when the top HALL_SIZE are all short.
        st3, ids3 = self._setup([(30 - k, True, 3, 10) for k in range(25)] + [(-1.0, True, 3, 10), (-2.0, True, 3, 10)])
        for nid in ids3[:25]: st3['nodes'][nid]['genes']['direction'] = 'short'
        for nid in ids3[25:]: st3['nodes'][nid]['genes']['direction'] = 'long'
        st3['hall'] = []; E.update_hall(st3)
        self.assertEqual(st3['hall'][:E.HALL_SIZE], ids3[:E.HALL_SIZE]); self.assertEqual(st3['hall'][-2:], ids3[25:])
        # With no qualified strategy of another direction, shorts keep every seat rather than leave one empty.
        st2, ids2 = self._setup([(9 - k, True, 3, 10) for k in range(5)])
        for nid in ids2: st2['nodes'][nid]['genes']['direction'] = 'short'
        E.update_hall(st2); E.choose_team(st2, T0)
        self.assertEqual(len(st2['team']), 5); self.assertEqual(E.choose_team(st2, T0 + (E.MIN_TENURE // h + 1) * h), [])

    def test_breakdown_replaces_a_member_at_once_and_migration_keeps_the_old_champion(self):
        st, ids = self._setup([(5.0, True, 3, 10), (4.0, True, 3, 10), (3.0, True, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k]
        E.update_hall(st)
        st['champion'], st['champion_since'] = ids[1], T0 - 5
        st.pop('team', None)
        E.choose_team(st, T0)
        self.assertEqual(st['team'][0], dict(id=ids[1], since=T0 - 5))        # migrated with its tenure
        st['nodes'][ids[1]]['test'].update(ret=-6.0); st['nodes'][ids[1]]['qualified'] = False; E.update_hall(st)
        st['nodes'][st['population'][3]].update(fitness=2.0, qualified=True, species='trend'); E.update_hall(st)
        changes = E.choose_team(st, T0 + 3600_000)
        self.assertIn((0, ids[1], st['population'][3], 'breakdown'), changes)
        self.assertFalse(E.breaks_retain(dict(test=dict(ret=0, max_dd=29))))
        self.assertTrue(E.breaks_retain(dict(test=dict(ret=5, max_dd=31))))
        self.assertFalse(E.breaks_retain(dict(test=dict(ret=-4, max_dd=10))))
        self.assertTrue(E.breaks_retain(dict(test=dict(ret=5, max_dd=10), final=dict(ret=-6, max_dd=10))))   # the final window counts too

    def test_final_window_is_a_second_gate_and_one_sided_strategies_lose_half_their_fitness(self):
        r = E.evaluate(base_genes(), self.sr, self.split)
        self.assertEqual(r['qualified'], r['test_ok'] and r['final_ok'])
        self.assertIn('trades', r['final']); self.assertIn('regimes', r['train'])
        three = E.evaluate(base_genes(), self.sr, self.split[:2] + self.split[3:])     # no final window: the old two-way split
        self.assertNotIn('final', three); self.assertEqual(three['qualified'], three['test_ok'])
        self.assertFalse(E.passes(dict(ret=5, max_dd=10, trades=3), E.FINAL_GATE)); self.assertTrue(E.passes(dict(ret=5, max_dd=10, trades=4), E.FINAL_GATE))
        # regime split: equity rises 1 % a day while BTC rises, falls 1 % a day while BTC falls
        day = 24 * H
        btc = series_from([100 + i * .01 for i in range(1500)] + [115 - i * .01 for i in range(1500)])
        curve = [(btc.ts[0] + d * day, 1000 * (1.01 ** min(d, 62)) * (.99 ** max(0, d - 62))) for d in range(125)]
        reg = E.regime_returns(curve, btc)
        self.assertGreater(reg['up'], 30); self.assertLess(reg['down'], -20)
        self.assertGreaterEqual(min(reg['up_days'], reg['down_days']), E.REGIME_MIN_DAYS)
        self.assertTrue(E.one_sided(reg))
        self.assertFalse(E.one_sided(dict(up=5, down=3, up_days=40, down_days=40)))
        self.assertFalse(E.one_sided(dict(up=5, down=-3, up_days=40, down_days=5)))     # too few falling days to judge

    def _breakout_market(self, coins=8, tail=30):
        closes = [100.0] * 40 + [110.0] + [111.0] * tail                       # every coin breaks out on bar 40
        return {f'C{k}-USDT-SWAP': series_from(list(closes), f'C{k}-USDT-SWAP') for k in range(coins)}

    def test_shared_account_with_one_strategy_matches_the_single_strategy_engine(self):
        loose = dict(X.ACCOUNT_RULES, same_side_cap=9, daily_halt=9, major_factor=1.0)
        for k in range(12):
            g = G.random_genome(random.Random(k), G.SPECIES[k % len(G.SPECIES)])
            a = X.simulate(g, self.sr, 60, 900, equity=5000.0, curve_every=1)
            b = X.simulate_account([dict(id='x', genes=g, from_i=0, wind_i=None, boosts=[])], self.sr, 60, 900, equity=5000.0, rules=loose)
            self.assertAlmostEqual(a['equity'], b['equity'], places=6, msg=g)
            self.assertEqual(len(a['trades']), len(b['trades'])); self.assertEqual(a['curve'], b['curve'])
            self.assertAlmostEqual(b['pnl']['x'], b['equity'] - 5000.0, places=6)

    @mock.patch.dict(X.ACCOUNT_RULES, PRIOR_ERA)   # sizing mechanics of the earlier eras: no trail/risk factors
    @STOP_RISK
    def test_shared_account_rules_one_position_per_coin_crowding_cap_leader_and_wind_down(self):
        sr = self._breakout_market(coins=13)
        g = base_genes(risk=.01)
        seg = lambda i, **kw: dict(dict(id=f'm{i}', genes=g, from_i=0, wind_i=None, boosts=[]), **kw)
        # crowding steps on their own (the count cap is tested below); a 1 % floor keeps 12 positions inside the margin
        nocap = dict(X.ACCOUNT_RULES, max_same_side=None, min_risk=.01)
        r = X.simulate_account([seg(1), seg(2), seg(3), seg(4)], sr, 30, 71, equity=10000.0, rules=nocap)
        pos = {p['inst']: p for p in r['positions']}
        self.assertEqual(len(pos), 12)                                                   # one position per coin, 3 per strategy at most
        self.assertEqual([sum(1 for p in pos.values() if p['seg'] == f'm{i}') for i in (1, 2, 3, 4)], [3, 3, 3, 3])
        risk = [round(p['qty'] * (p['entry'] - p['stop']) / 10000.0, 4) for p in r['positions']]
        for x in risk[:10]: self.assertAlmostEqual(x, .01, delta=.0005)                  # the first ten longs: full size
        for x in risk[10:]: self.assertAlmostEqual(x, .005, delta=.0005)                 # 10+ longs already open: half
        self.assertEqual(X.CROWD_STEPS, ((10, .5),))
        self.assertEqual([X.crowd_factor(n) for n in (0, 3, 6, 9, 10, 19)], [1.0, 1.0, 1.0, 1.0, .5, .5])
        self.assertEqual(X.crowd_factor(6, X.LEGACY_SIZING['crowd_steps']), .25)         # the pre-2026-11-20 steps, replay only
        # same-side risk: no total cap by default (2026-11-15); an explicit same_side_cap still bounds it
        g5 = base_genes(risk=.05)
        self.assertIsNone(X.ACCOUNT_RULES['same_side_cap']); self.assertEqual(X.ACCOUNT_RULES['daily_halt'], .25)
        r5 = X.simulate_account([dict(id=f'm{i}', genes=g5, from_i=0, wind_i=None, boosts=[]) for i in range(4)], sr, 30, 71, equity=10000.0)
        total = sum(p['qty'] * (p['entry'] - p['stop']) for p in r5['positions']) / 10000.0
        self.assertGreater(total, .205)
        capped = dict(X.ACCOUNT_RULES, same_side_cap=.20)
        r5c = X.simulate_account([dict(id=f'm{i}', genes=g5, from_i=0, wind_i=None, boosts=[]) for i in range(4)], sr, 30, 71, equity=10000.0, rules=capped)
        total = sum(p['qty'] * (p['entry'] - p['stop']) for p in r5c['positions']) / 10000.0
        self.assertLessEqual(total, .205); self.assertGreater(total, .10)
        self.assertTrue(all(p['qty'] * (p['entry'] - p['stop']) / 10000.0 <= X.MAX_TRADE_RISK + .002 for p in r5['positions']))
        # the leader uses its own risk/leverage genes (a 1 % gene → the 5 % floor), counts toward the same-side cap, and cannot exceed 20×
        # (leader_risk / max_risk = 10 % are caps, not a bigger size)
        lead = X.simulate_account([seg(1, boosts=[(0, None)]), seg(2, genes=g5)], sr, 30, 71, equity=10000.0, rules=capped)
        lp = [p for p in lead['positions'] if p['boost']]
        self.assertTrue(lp)
        self.assertAlmostEqual(lp[0]['qty'] * (lp[0]['entry'] - lp[0]['stop']) / 10000.0, X.clamp_risk(g['risk']), delta=.002)
        self.assertLessEqual(lp[0]['lever'], X.LEVERAGE)
        self.assertEqual((X.ACCOUNT_RULES['leader_risk'], X.ACCOUNT_RULES['leader_lever'], X.ACCOUNT_RULES['max_risk']), (.10, 20, .10))
        self.assertEqual(X.ACCOUNT_RULES['min_risk'], .05)
        total_lead = sum(p['qty'] * (p['entry'] - p['stop']) for p in lead['positions']) / 10000.0
        self.assertLessEqual(total_lead, .205)
        # wind-down: a segment that lost its seat at bar 45 keeps its positions but takes no new signal
        w = X.simulate_account([seg(1, wind_i=45)], sr, 30, 71, equity=10000.0)
        self.assertEqual(len(w['positions']), 3)
        self.assertEqual(X.simulate_account([seg(1, wind_i=40)], sr, 30, 71, equity=10000.0)['positions'], [])   # wound before the signal bar

    def test_same_side_full_boundaries(self):
        self.assertEqual(X.MAX_SAME_SIDE_POSITIONS, 20); self.assertEqual(X.ACCOUNT_RULES['max_same_side'], 20)
        self.assertEqual([X.same_side_full(n) for n in (0, 19, 20, 21)], [False, False, True, True])   # 19 open: one more fits; 20: full
        self.assertEqual([X.same_side_full(n, 1) for n in (0, 1)], [False, True])
        self.assertFalse(X.same_side_full(50, None)); self.assertFalse(X.same_side_full(50, 0))       # None / 0 = off

    def _two_sided_market(self, ups=8, downs=2):
        up = [100.0] * 40 + [110.0] + [111.0] * 30                                     # breaks out upward on bar 40 → long
        down = [100.0] * 40 + [90.0] + [89.0] * 30                                     # breaks down on bar 40 → short
        sr = {f'U{k}-USDT-SWAP': series_from(list(up), f'U{k}-USDT-SWAP') for k in range(ups)}
        sr.update({f'D{k}-USDT-SWAP': series_from(list(down), f'D{k}-USDT-SWAP') for k in range(downs)})
        return sr

    def test_same_side_count_blocks_new_entries_on_a_full_side_only(self):
        sr = self._two_sided_market(ups=22)
        g = base_genes(risk=.01)
        segs = [dict(id=f'm{i}', genes=g, from_i=0, wind_i=None, boosts=[]) for i in range(8)]   # 24 free slots: only the cap binds
        side = lambda r, d: sum(1 for p in r['positions'] if p['side'] == d)
        small = dict(X.ACCOUNT_RULES, min_risk=.01)                  # 1 % sizes: 22 positions fit into the margin, only the count binds
        r = X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=small)
        self.assertEqual((side(r, 1), side(r, -1)), (20, 2))         # 22 long signals: 20 taken; the 2 shorts are not affected
        # boundaries: cap 21 → 21 longs, cap 22 (= the signals) → all 22, off → all 22; a cap of 2 also fills the short side exactly
        for cap, want in ((21, (21, 2)), (22, (22, 2)), (None, (22, 2)), (2, (2, 2))):
            rc = X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=dict(small, max_same_side=cap))
            self.assertEqual((side(rc, 1), side(rc, -1)), want, cap)
        # the leader counts too: with the leader seated the long side still stops at 20
        lead = X.simulate_account([dict(segs[0], boosts=[(0, None)])] + segs[1:], sr, 30, 71, equity=10000.0, rules=small)
        self.assertEqual(side(lead, 1), 20)
        # side_cap_from: fills happen on bar 41 (signal on bar 40's close); the cap applies to fills after that bar
        self.assertEqual(side(X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=small, side_cap_from=40), 1), 20)
        self.assertEqual(side(X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=small, side_cap_from=41), 1), 22)

    def test_same_side_count_never_blocks_exits(self):
        sr = self._two_sided_market(ups=22, downs=0)
        g = base_genes(risk=.01, max_hold=5)
        segs = [dict(id=f'm{i}', genes=g, from_i=0, wind_i=None, boosts=[]) for i in range(8)]
        r = X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=dict(X.ACCOUNT_RULES, min_risk=.01), exit_from=71)   # old exits (time)
        self.assertEqual(r['positions'], [])                         # the full long side still runs every position to its exit
        self.assertEqual([t['reason'] for t in r['trades']], ['time'] * 20)

    def test_paper_account_records_when_the_same_side_count_started(self):
        acc = dict(from_ts=T0, start_equity=10000.0, segs=[], next=0)
        sr = self._two_sided_market()
        state = dict(nodes={}, team=[], paper=dict(account=acc, start_equity=10000.0))
        P.update(state, sr, [])
        self.assertEqual(acc['side_cap_from'], sr['U0-USDT-SWAP'].ts[-1])
        acc['side_cap_from'] = T0                                    # an earlier start is kept, not moved forward
        P.update(state, sr, [])
        self.assertEqual(acc['side_cap_from'], T0)

    @mock.patch.dict(X.ACCOUNT_RULES, PRIOR_ERA)   # sizing mechanics of the earlier eras: no trail/risk factors
    @STOP_RISK
    def test_majors_risk_twice_as_much_in_the_shared_account(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 30
        sr = {k: series_from(list(closes), k) for k in ('BTC-USDT-SWAP', 'PEPE-USDT-SWAP')}
        lev = dict(leverage=20, stop_atr=5.0)                                       # wide stops, 20×: the margin does not bind
        r = X.simulate_account([dict(id='m', genes=base_genes(risk=.05, **lev), from_i=0, wind_i=None, boosts=[])], sr, 30, 71, equity=10000.0)
        risk = {p['inst']: round(p['qty'] * (p['entry'] - p['stop']) / 10000.0, 3) for p in r['positions']}
        # 5 % × 2 is 10 %, exactly the 10 % cap
        self.assertEqual(risk, {'BTC-USDT-SWAP': .10, 'PEPE-USDT-SWAP': .05})
        r2 = X.simulate_account([dict(id='m', genes=base_genes(risk=.08, **lev), from_i=0, wind_i=None, boosts=[])], sr, 30, 71, equity=10000.0)
        risk2 = {p['inst']: round(p['qty'] * (p['entry'] - p['stop']) / 10000.0, 3) for p in r2['positions']}
        self.assertEqual(risk2, {'BTC-USDT-SWAP': .10, 'PEPE-USDT-SWAP': .08})   # 8 % × 2 is clamped to 10 %
        self.assertEqual(X.ACCOUNT_RULES['major_factor'], 2.0)
        self.assertEqual(X.MAJORS, ('BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'BNB-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP'))
        self.assertEqual(X.ACCOUNT_RULES['majors'], X.MAJORS)
        self.assertEqual(X.position_risk(.05, 'BNB-USDT-SWAP'), .10)
        self.assertEqual(X.position_risk(.05, 'DOGE-USDT-SWAP'), .10)
        self.assertEqual(X.position_risk(.08, 'BTC-USDT-SWAP'), .10)
        self.assertEqual(X.position_risk(.15, 'BTC-USDT-SWAP'), .10)
        self.assertEqual(X.position_risk(.05, 'PEPE-USDT-SWAP'), .05)

    @STOP_RISK
    def test_trade_risk_is_kept_inside_5_to_10_percent_with_crowding_at_10(self):
        self.assertEqual((X.MIN_TRADE_RISK, X.MAX_TRADE_RISK), (.05, .10))
        self.assertEqual((X.ACCOUNT_RULES['min_risk'], X.ACCOUNT_RULES['max_risk'], X.ACCOUNT_RULES['leader_risk']), (.05, .10, .10))
        self.assertEqual(X.CROWD_STEPS, ((10, .5),))
        pr = lambda *a: round(X.position_risk(*a), 6)
        self.assertEqual(pr(None, 'PEPE-USDT-SWAP'), .05)                     # missing risk → 5 %
        self.assertEqual(pr(.02, 'PEPE-USDT-SWAP'), .05)                      # an old 2 % gene → the 5 % floor
        self.assertEqual(pr(.25, 'PEPE-USDT-SWAP'), .10)                      # above the band → 10 %
        self.assertEqual(pr(.12, 'PEPE-USDT-SWAP'), .10)                      # an old 5–20 % gene above 10 % → 10 %
        self.assertEqual(pr(.08, 'PEPE-USDT-SWAP', 9), .08)                   # 9 already open on the side: full
        self.assertEqual(pr(.08, 'PEPE-USDT-SWAP', 10), .04)                  # 10 already open: half
        self.assertEqual(pr(.05, 'PEPE-USDT-SWAP', 10), .025)                 # the floor is on the gene; crowding may go below it
        self.assertEqual(pr(.10, 'BTC-USDT-SWAP', 10), .10)                   # half × major 2 stays within 10 %
        self.assertEqual((X.clamp_risk(None), X.clamp_risk(.01), X.clamp_risk(.3), X.clamp_risk(.07)), (.05, .05, .10, .07))
        legacy = dict(X.ACCOUNT_RULES, **X.LEGACY_SIZING)                     # the old sizing still reproduces the old numbers
        self.assertEqual((pr(.02, 'PEPE-USDT-SWAP', 0, legacy), pr(.02, 'PEPE-USDT-SWAP', 3, legacy), pr(.08, 'BTC-USDT-SWAP', 6, legacy)), (.02, .01, .04))
        self.assertEqual(X.LEGACY_SIZING, dict(min_risk=.02, max_risk=.10, crowd_steps=((6, .25), (3, .5))))
        prior = dict(X.ACCOUNT_RULES, **X.PRIOR_SIZING)                       # ... and so does the PR #107 sizing (5–20 %, cap 20 %, half at 10)
        self.assertEqual(X.PRIOR_SIZING, dict(min_risk=.05, max_risk=.20, crowd_steps=((10, .5),)))
        self.assertEqual((pr(.12, 'PEPE-USDT-SWAP', 0, prior), pr(.12, 'PEPE-USDT-SWAP', 10, prior), pr(.25, 'PEPE-USDT-SWAP', 0, prior),
                          pr(.15, 'BTC-USDT-SWAP', 0, prior), pr(.08, 'BTC-USDT-SWAP', 0, prior), pr(.02, 'PEPE-USDT-SWAP', 0, prior)), (.12, .06, .20, .20, .16, .05))
        # the single-strategy engine (fitness) uses the same floor: a 2 % gene is sized at 5 %
        sr = {'PEPE-USDT-SWAP': series_from([100.0] * 40 + [110.0] + [111.0] * 30, 'PEPE-USDT-SWAP')}
        lo = X.simulate(base_genes(risk=.02, leverage=20, stop_atr=5.0), sr, 30, 71, equity=10000.0)['positions'][0]
        self.assertAlmostEqual(lo['qty'] * (lo['entry'] - lo['stop']) / 10000.0, .05, delta=.001)

    @mock.patch.dict(X.ACCOUNT_RULES, PRIOR_ERA)   # sizing mechanics of the earlier eras: no trail/risk factors
    @STOP_RISK
    def test_risk_from_keeps_the_size_of_fills_made_before_the_sizing_change(self):
        sr = self._breakout_market(coins=12)
        g = base_genes(risk=.12, leverage=20, stop_atr=5.0)                    # new band; the seat's stored gene was 2 %
        segs = [dict(id=f'm{i}', genes=g, old_risk=.02, from_i=0, wind_i=None, boosts=[]) for i in range(4)]
        rules = dict(X.ACCOUNT_RULES, max_same_side=None)
        size = lambda r: [round(p['qty'] * (p['entry'] - p['stop']) / 10000.0, 3) for p in r['positions']]
        # fills happen on bar 41: with risk_from=41 they are pre-change fills and keep the old size and old crowd steps
        old = X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=rules, risk_from=41)
        self.assertEqual(size(old)[:3], [.02, .02, .02]); self.assertEqual(size(old)[3:6], [.01, .01, .01])
        self.assertTrue(all(abs(x - .005) < .0006 for x in size(old)[6:]))
        # risk_from=40: the same fills are after the change and use the current gene band (12 % → the 10 % cap) — until the margin runs out
        new = X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=rules, risk_from=40)
        self.assertTrue(new['positions']); self.assertTrue(all(abs(x - .10) < .002 for x in size(new)))
        self.assertEqual(size(X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=rules)), size(new))   # None = new rules everywhere
        # risk_from=41 wins over a later risk_cap_from: fills on or before risk_from are always the legacy era
        self.assertEqual(size(X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=rules, risk_from=41, risk_cap_from=50)), size(old))

    @mock.patch.dict(X.ACCOUNT_RULES, PRIOR_ERA)   # sizing mechanics of the earlier eras: no trail/risk factors
    @STOP_RISK
    def test_risk_cap_from_keeps_the_size_of_fills_made_under_the_5_to_20_percent_rules(self):
        sr = self._breakout_market(coins=12)
        g = base_genes(risk=.12, leverage=20, stop_atr=5.0)                    # a seat stored under the 5–20 % band at 12 %
        segs = [dict(id=f'm{i}', genes=g, old_risk=.12, from_i=0, wind_i=None, boosts=[]) for i in range(4)]
        rules = dict(X.ACCOUNT_RULES, max_same_side=None)
        size = lambda r: [round(p['qty'] * (p['entry'] - p['stop']) / 10000.0, 3) for p in r['positions']]
        # fills happen on bar 41: after risk_from (40) and on or before risk_cap_from (41) → PR #107 rules, the 12 % size is kept
        mid = X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=rules, risk_from=40, risk_cap_from=41)
        self.assertTrue(mid['positions']); self.assertTrue(all(abs(x - .12) < .002 for x in size(mid)))
        # risk_cap_from=40: the same fills come after the new stamp and are capped at 10 %
        new = X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=rules, risk_from=39, risk_cap_from=40)
        self.assertTrue(new['positions']); self.assertTrue(all(abs(x - .10) < .002 for x in size(new)))
        # risk_from None: the mid era starts at the account's first bar
        self.assertEqual(size(X.simulate_account(segs, sr, 30, 71, equity=10000.0, rules=rules, risk_cap_from=41)), size(mid))
        # a major filled under the 5–20 % rules keeps its doubled size (10 % × 2 = 20 %); after the new stamp it is 10 %
        closes = [100.0] * 40 + [110.0] + [111.0] * 30
        mk = {k: series_from(list(closes), k) for k in ('BTC-USDT-SWAP', 'PEPE-USDT-SWAP')}
        seg = [dict(id='m', genes=base_genes(risk=.10, leverage=20, stop_atr=5.0), old_risk=.10, from_i=0, wind_i=None, boosts=[])]
        risk = lambda r: {p['inst']: round(p['qty'] * (p['entry'] - p['stop']) / 10000.0, 3) for p in r['positions']}
        self.assertEqual(risk(X.simulate_account(seg, mk, 30, 71, equity=10000.0, risk_from=40, risk_cap_from=41)), {'BTC-USDT-SWAP': .20, 'PEPE-USDT-SWAP': .10})
        self.assertEqual(risk(X.simulate_account(seg, mk, 30, 71, equity=10000.0, risk_from=39, risk_cap_from=40)), {'BTC-USDT-SWAP': .10, 'PEPE-USDT-SWAP': .10})

    def test_paper_account_records_when_the_new_risk_rules_started(self):
        acc = dict(from_ts=T0, start_equity=10000.0, segs=[], next=0)
        sr = self._two_sided_market()
        state = dict(nodes={}, team=[], paper=dict(account=acc, start_equity=10000.0))
        view, _ = P.update(state, sr, [])
        self.assertEqual(acc['risk_rules_from'], sr['U0-USDT-SWAP'].ts[-1])
        self.assertEqual(acc['risk_cap_from'], sr['U0-USDT-SWAP'].ts[-1])
        self.assertEqual((view['rules']['min_risk'], view['rules']['max_risk'], view['rules']['leader_risk']), (.05, .10, .10))
        acc['risk_rules_from'] = T0                                  # an earlier start is kept, not moved forward
        acc['risk_cap_from'] = T0 + H
        P.update(state, sr, [])
        self.assertEqual((acc['risk_rules_from'], acc['risk_cap_from']), (T0, T0 + H))
        # an account stamped by PR #107 only: risk_rules_from stays, risk_cap_from is added once at this run's last bar
        del acc['risk_cap_from']
        P.update(state, sr, [])
        self.assertEqual((acc['risk_rules_from'], acc['risk_cap_from']), (T0, sr['U0-USDT-SWAP'].ts[-1]))
        P.update(state, sr, [])
        self.assertEqual((acc['risk_rules_from'], acc['risk_cap_from']), (T0, sr['U0-USDT-SWAP'].ts[-1]))

    @mock.patch.dict(X.ACCOUNT_RULES, PRIOR_ERA)   # sizing mechanics of the earlier eras: no trail/risk factors
    def test_paper_replay_keeps_the_size_of_positions_filled_under_the_5_to_20_percent_rules(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 30                    # breakout on bar 40, fills on bar 41
        sr = {k: series_from(list(closes), k) for k in ('BTC-USDT-SWAP', 'PEPE-USDT-SWAP')}
        last = sr['BTC-USDT-SWAP'].ts[-1]
        stored = base_genes(risk=.12, leverage=20, stop_atr=5.0)          # the seat's genes as stored under the 5–20 % band

        def replay(cap_from):
            acc = dict(from_ts=T0 + 30 * H, start_equity=10000.0, next=1, side_cap_from=T0, risk_rules_from=T0 + 30 * H,
                       segs=[dict(id='a1', champion='n1', slot=0, genes=dict(stored), species='momentum', from_ts=T0 + 30 * H,
                                  wind_ts=None, boosts=[], reason='carry')],
                       **({'risk_cap_from': cap_from} if cap_from is not None else {}))
            state = dict(nodes={'n1': dict(genes=dict(stored), species='momentum', fitness=1.0)}, team=[], paper=dict(account=acc, start_equity=10000.0))
            view, _ = P.update(state, sr, [])
            self.assertEqual(acc['risk_rules_from'], T0 + 30 * H)                            # the PR #107 stamp never moves
            self.assertEqual(acc['segs'][0]['genes']['risk'], .12)                           # the stored gene is not rewritten
            return acc, {p['inst']: round(p['qty'] * (p['entry'] - p['stop']) / 10000.0, 3) for p in view['positions']}

        # filled (bar 41) before the new stamp: the open positions keep their 5–20 % size (12 %, major 12 % × 2 → 20 % cap)
        acc, kept = replay(None)                                           # first run with the change: the stamp is this run's last bar
        self.assertEqual(acc['risk_cap_from'], last)
        self.assertEqual(kept, {'BTC-USDT-SWAP': .20, 'PEPE-USDT-SWAP': .12})
        self.assertEqual(replay(last)[1], kept)                            # every later replay gives the same size
        # filled after the new stamp: 5–10 %, cap 10 % (major × 2 included)
        self.assertEqual(replay(T0 + 40 * H)[1], {'BTC-USDT-SWAP': .10, 'PEPE-USDT-SWAP': .10})

    def test_coins_whose_demo_price_is_off_take_no_new_entry_and_the_ranges_are_remembered(self):
        sr = self._breakout_market(coins=3)
        seg = [dict(id='m', genes=base_genes(risk=.02), from_i=0, wind_i=None, boosts=[])]
        r = X.simulate_account(seg, sr, 30, 71, equity=10000.0, blocked={'C1-USDT-SWAP': [(35, None)], 'C2-USDT-SWAP': [(10, 38)]})
        self.assertEqual(sorted(p['inst'] for p in r['positions']), ['C0-USDT-SWAP', 'C2-USDT-SWAP'])    # C1 blocked at the fill; C2's block had ended
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'evolve'; path.mkdir()
            (path / 'universe.json').write_text(json.dumps(dict(version=2, instruments=['A-USDT-SWAP', 'B-USDT-SWAP', 'C-USDT-SWAP'])))
            tick = lambda **px: [dict(instId=f'{k}-USDT-SWAP', last=str(v)) for k, v in px.items()]
            ranges, now = D.price_check(tmp, 1000, lambda p: tick(A=100, B=100, C=100), lambda p: tick(A=100.5, B=147, C=0))
            self.assertEqual(ranges, {'B-USDT-SWAP': [[1000, None]], 'C-USDT-SWAP': [[1000, None]]}); self.assertEqual(now, {'B-USDT-SWAP': .47, 'C-USDT-SWAP': None})
            ranges, now = D.price_check(tmp, 2000, lambda p: tick(A=100, B=100, C=100), lambda p: tick(A=103, B=100.2, C=0))
            self.assertEqual(ranges, {'A-USDT-SWAP': [[2000, None]], 'B-USDT-SWAP': [[1000, 2000]], 'C-USDT-SWAP': [[1000, None]]})   # B is back, A drifted
            self.assertEqual(json.loads((path / 'universe.json').read_text())['price_off'], ranges)
            def down(p): raise RuntimeError('x')
            self.assertEqual(D.price_check(tmp, 3000, down, down)[0], ranges)                             # the check failing changes nothing
        # the paper account: a blocked coin's pending signal is neither shown nor sent to the mirror
        st, ids = self._setup([(5.0, True, 3, 10)])
        E.update_hall(st); E.choose_team(st, T0)
        self._grant_entry(st, ids[0])
        view, signals = P.update(st, self.sr, [])
        view2, signals2 = P.update(st, self.sr, [], {k: [[self.sr[k].ts[0], None]] for k in self.sr})
        self.assertEqual(view2['price_off'], sorted(self.sr)); self.assertEqual(view2['pending'], []); self.assertEqual(signals2, {})

    def test_pending_signals_stop_at_a_strategys_free_position_slots(self):
        closes = [100.0] * 40 + [110.0]                                        # the data ends on the breakout bar: every coin is pending
        sr = {f'C{k}-USDT-SWAP': series_from(list(closes), f'C{k}-USDT-SWAP') for k in range(6)}
        st, ids = self._setup([(5.0, True, 3, 10)])
        st['nodes'][ids[0]]['genes'] = base_genes(risk=.02)
        st['team'] = [dict(id=ids[0], since=0)]; st['champion'] = st['leader'] = None
        self._grant_entry(st, ids[0])
        st['paper'] = dict(start_equity=10000.0, account=dict(from_ts=sr['C0-USDT-SWAP'].ts[30], start_equity=10000.0, next=0, segs=[]))
        view, signals = P.update(st, sr, [])
        self.assertEqual(len(view['pending']), X.MAX_POSITIONS); self.assertEqual(len(signals), X.MAX_POSITIONS)   # six signals, three free slots
        self.assertEqual(sorted(signals), [f'C{k}-USDT-SWAP' for k in range(3)])                                     # the ones the account fills

    def test_daily_halt_closes_everyone_including_the_leader(self):
        closes = [100.0] * 40 + [110.0, 111.0, 111.0] + [104.0] * 20                     # breakout, then a fall that stays above the stops
        sr = {f'C{k}-USDT-SWAP': series_from(list(closes), f'C{k}-USDT-SWAP') for k in range(6)}
        g = base_genes(risk=.05, stop_atr=6.0)
        segs = [dict(id='lead', genes=base_genes(risk=.02, stop_atr=6.0), from_i=0, wind_i=None, boosts=[(0, None)])] + \
               [dict(id=f'm{i}', genes=g, from_i=0, wind_i=None, boosts=[]) for i in range(2)]
        r = X.simulate_account(segs, sr, 30, len(closes), equity=10000.0, rules=dict(X.ACCOUNT_RULES, daily_halt=.03))
        halted = [t for t in r['trades'] if t['reason'] == 'daily_halt']
        self.assertTrue(halted)
        self.assertTrue(any(t['boost'] for t in halted))                                 # the leader is closed too
        self.assertFalse(r['positions'])                                                 # nobody keeps a position
        # halted may clear at the next UTC+7 midnight; the closes themselves are the evidence

    def test_paper_account_is_shared_converts_the_old_slots_and_follows_seat_and_leader_changes(self):
        st, ids = self._setup([(5.0, True, 3, 10), (4.0, True, 3, 10), (3.0, True, 3, 10), (2.0, True, 3, 10), (1.0, True, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % 4]
        E.update_hall(st)
        self._grant_entry(st, ids[0])   # only a member that passed the 5-gen entry gate may take a live paper seat
        E.choose_team(st, T0)
        n = self.sr['BTC-USDT-SWAP'].n
        cut = lambda back: {k: X.Series(k, v.ts[:n - back], v.o[:n - back], v.h[:n - back], v.l[:n - back], v.c[:n - back]) for k, v in self.sr.items()}
        st['paper'] = dict(start_equity=10000.0, slots=[dict(P._new_slot(2100.0), curve_done=[[T0, 2000.0], [T0 + H, 2100.0]]) for _ in range(5)])
        view, signals = P.update(st, cut(300), [])
        acc = st['paper']['account']
        self.assertNotIn('slots', st['paper']); self.assertEqual(acc['start_equity'], 10500.0)        # the slots' total carries over
        self.assertEqual(view['equity'], 10500.0); self.assertEqual(view['mode'], 'cross')
        self.assertEqual((view['liquidations'], view['insurance'], view['adjustments']), (0, 0.0, []))     # published for the dashboard
        self.assertEqual(view['curve'][-2:], [[T0 + H, 10500.0], [cut(300)['BTC-USDT-SWAP'].ts[-1], 10500.0]])   # old curve kept, new one continues
        live = [g for g in acc['segs'] if g['wind_ts'] is None]
        self.assertEqual([g['champion'] for g in live], [ids[0]])
        self.assertEqual([m['id'] for m in view['members']], [ids[0]])
        self.assertTrue(live[0]['boosts'])                                                           # the one live member is the leader
        view, signals = P.update(st, cut(150), [])                                                       # 150 bars later
        self.assertAlmostEqual(view['equity'], 10500.0 + sum(m['pnl'] for m in view['members']) + view['insurance'], places=1)
        self.assertEqual(len({p['inst'] for p in view['positions']}), len(view['positions']))           # one position per coin
        for inst, sig in signals.items():
            self.assertLessEqual(sig['leverage'], X.LEVERAGE)
            self.assertGreaterEqual(sig['leverage'], 1)
            self.assertLessEqual(sig['risk'], X.MAX_TRADE_RISK)
            self.assertGreaterEqual(sig['risk'], X.MIN_TRADE_RISK)
        # a seat change to someone who has not passed the entry gate does not give them a live seat;
        # the incumbent keeps trading, nothing is sold at market (no 'switch' closes).
        new = next(nid for nid in st['population'] if nid not in ids)
        st['team'][1] = dict(id=new, since=0)
        view2, _ = P.update(st, cut(150), [(1, ids[1], new, 'challenger')])
        self.assertFalse(any(g['champion'] == new and g['wind_ts'] is None for g in acc['segs']))
        self.assertAlmostEqual(view2['equity'], view['equity'], places=2)
        view3, _ = P.update(st, self.sr, [])
        self.assertFalse([t for t in view3['trades'] if t['reason'] == 'switch'])

    def test_leader_title_is_sticky(self):
        st, ids = self._setup([(5.0, True, 3, 10), (4.0, True, 3, 10), (3.0, True, 3, 10), (2.0, True, 3, 10), (1.0, True, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % 4]
        E.update_hall(st); E.choose_team(st, T0)
        for nid in ids:
            self._grant_entry(st, nid)   # leader boost requires the three-part entry gate, not only OOS
        self.assertEqual(st['champion'], ids[0])
        st['nodes'][ids[1]]['fitness'] = 9.0
        E.choose_team(st, T0 + 3600_000)
        self.assertEqual(st['champion'], ids[0])                                      # better fitness, but tenure has not passed
        E.choose_team(st, T0 + E.MIN_TENURE + 3600_000)
        self.assertEqual(st['champion'], ids[1])
        # the bigger size is its own title: only for a member that passed both gates
        self.assertEqual(st['leader'], ids[1]); self.assertEqual(P.LEADER_RISK, .10)
        st['nodes'][ids[1]]['qualified'] = False
        E.choose_team(st, T0 + E.MIN_TENURE + 2 * 3600_000)
        self.assertEqual(st['leader'], ids[0])                                         # lost at once, the best qualified member takes it
        self.assertEqual(st['champion'], ids[1])                                       # (the display leader is unchanged)
        for nid in ids: st['nodes'][nid]['qualified'] = False
        E.choose_team(st, T0 + E.MIN_TENURE + 3 * 3600_000)
        self.assertIsNone(st['leader'])                                                # nobody qualified: nobody trades bigger
        view, signals = P.update(st, self.sr, [])
        self.assertIsNone(view['leader']); self.assertFalse(any(m['boost'] for m in view['members']))
        self.assertFalse(any(s.get('leader') for s in signals.values()))

    def test_prune_keeps_champion_ancestry(self):
        st = E.init_state(3, self.sr, self.split)
        for _ in range(3): E.generation(st, self.sr, self.split, self.obs)
        champ = next(n for n in st['nodes'].values() if n['parents'])
        st['champion'] = champ['id']; st['generation'] = 50
        E.prune(st)
        self.assertIn(champ['id'], st['nodes'])
        for p in champ['parents']: self.assertIn(p, st['nodes'])


class EntryGateTests(unittest.TestCase):
    def setUp(self):
        self.sr = tiny_market(); self.split = D.split(self.sr)
        self.obs = D.observe_volatility(self.sr)

    def test_defense_streak_matches_old_candidate_streak(self):
        s, prev = 0, None
        seq = [('a', True)] * 4 + [('b', True)] + [('b', True)] * 4 + [('b', False), ('b', True)]
        out = []
        for cid, ok in seq:
            s = E.update_defense_streak(prev, s, cid, ok); prev = cid; out.append(s)
        self.assertEqual(out, [1, 2, 3, 4, 1, 2, 3, 4, 5, 0, 1])
        self.assertEqual(E.ENTRY_STREAK, 5)
        self.assertEqual(E.ENTRY_MAX_DD, 30.0)

    def test_entry_gate_requires_oos_streak_and_drawdown(self):
        node = dict(id='s1', qualified=True, test=dict(ret=5, max_dd=10, trades=8), final=dict(ret=2, max_dd=8, trades=4), oos_dd=12.0)
        g = E.entry_gate(node, streak=5)
        self.assertTrue(g['passed']); self.assertEqual(g['reasons'], []); self.assertTrue(g['oos_ok'])
        self.assertEqual(g['streak'], 5); self.assertEqual(g['max_dd'], 12.0)
        fail_oos = E.entry_gate(dict(node, qualified=False), streak=5)
        self.assertFalse(fail_oos['passed']); self.assertTrue(any('样本外' in r for r in fail_oos['reasons']))
        fail_streak = E.entry_gate(node, streak=4)
        self.assertFalse(fail_streak['passed']); self.assertTrue(any('守擂' in r for r in fail_streak['reasons']))
        fail_dd = E.entry_gate(dict(node, oos_dd=30.1), streak=5)
        self.assertFalse(fail_dd['passed']); self.assertTrue(any('回撤' in r for r in fail_dd['reasons']))
        self.assertTrue(E.entry_gate(dict(node, oos_dd=30.0), streak=5)['passed'])   # 30 % inclusive

    def test_five_generations_in_one_run_can_fill_the_streak(self):
        st = dict(population=['a', 'b'], nodes={
            'a': dict(id='a', fitness=2, qualified=True),
            'b': dict(id='b', fitness=1, qualified=True),
        }, defense_id=None, defense_streak=0)
        for _ in range(5):
            E.record_defense(st)
        self.assertEqual((st['defense_id'], st['defense_streak']), ('a', 5))
        st['nodes']['b']['fitness'] = 3
        E.record_defense(st)
        self.assertEqual((st['defense_id'], st['defense_streak']), ('b', 1))
        st['nodes']['b']['qualified'] = False
        E.record_defense(st)
        self.assertEqual(st['defense_streak'], 0)
        # generation() itself updates the defense record
        st2 = E.init_state(11, self.sr, self.split)
        E.generation(st2, self.sr, self.split, self.obs)
        self.assertIn('defense_id', st2)
        self.assertIn(st2['defense_id'], st2['population'])

    def test_failing_members_are_wound_and_an_empty_book_holds_cash(self):
        ev = EvolutionTests()
        ev.sr, ev.split, ev.obs = self.sr, self.split, self.obs
        ev.addCleanup = lambda *a, **k: None
        ev.setUp()
        st, ids = ev._setup([(5.0, True, 3, 10), (4.0, True, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % 4]
        E.update_hall(st); E.choose_team(st, T0)
        # nobody has a 5-gen defense streak yet: every seat is wound, no new signals, cash is held
        view, signals = P.update(st, self.sr, [])
        self.assertFalse(any(g['wind_ts'] is None for g in st['paper']['account']['segs']))
        self.assertEqual(view['pending'], [])
        self.assertFalse(any(s.get('pending') for s in signals.values()))
        ev._grant_entry(st, ids[0])
        view2, signals2 = P.update(st, self.sr, [])
        live = [g for g in st['paper']['account']['segs'] if g['wind_ts'] is None]
        self.assertEqual([g['champion'] for g in live], [ids[0]])
        self.assertTrue(view2['members'])
        # a node that fails DD never gets the seat, even with a full streak
        st['nodes'][ids[0]]['oos_dd'] = 31
        view3, _ = P.update(st, self.sr, [])
        self.assertFalse(any(g['champion'] == ids[0] and g['wind_ts'] is None for g in st['paper']['account']['segs']))
        self.assertFalse([t for t in view3['trades'] if t['reason'] == 'switch'])


    def test_each_seat_has_its_own_defense_streak_persisted_and_refresh_does_not_increment(self):
        ev = EvolutionTests()
        ev.sr, ev.split, ev.obs = self.sr, self.split, self.obs
        ev.addCleanup = lambda *a, **k: None
        ev.setUp()
        st, ids = ev._setup([(5.0, True, 3, 10), (4.0, True, 3, 10), (3.0, True, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % 4]
        E.update_hall(st); E.choose_team(st, T0)
        self.assertGreaterEqual(len(st['team']), 2)
        a, b = st['team'][0]['id'], st['team'][1]['id']
        for _ in range(3):
            E.record_defense(st)
        self.assertEqual(st['defense_streaks'][a], 3)
        self.assertEqual(st['defense_streaks'][b], 3)
        self.assertEqual(E.entry_gate(st['nodes'][a], state=st)['streak'], 3)
        # persist across a reload the way a later Actions run would
        st2 = json.loads(json.dumps(st))
        self.assertEqual(st2['defense_streaks'][a], 3)
        E.refresh(st2, self.sr, self.split)
        self.assertEqual(st2['defense_streaks'].get(a), 3)       # refresh must not add (or clear) a generation
        self.assertEqual(st2['defense_streaks'].get(b), 3)
        # keep using the scripted roster to check later increments and resets
        st3 = json.loads(json.dumps(st))
        E.choose_team(st3, T0 + 3600_000)
        self.assertEqual(st3['defense_streaks'][a], 3)           # choose_team must not add a generation
        for _ in range(2):
            E.record_defense(st3)
        self.assertEqual(st3['defense_streaks'][a], 5)
        self.assertEqual(st3['defense_streaks'][b], 5)
        self.assertTrue(E.entry_gate(st3['nodes'][a], state=st3)['passed'])
        # fail OOS: that seat resets; the other is still qualified so it increments one more
        st3['nodes'][a]['qualified'] = False
        E.record_defense(st3)
        self.assertEqual(st3['defense_streaks'][a], 0)
        self.assertEqual(st3['defense_streaks'][b], 6)
        # drop off the team: leftover count is cleared
        st3['nodes'][a]['qualified'] = True
        st3['defense_streaks'][a] = 4
        st3['team'] = [m for m in st3['team'] if m['id'] != a]
        E.record_defense(st3)
        self.assertEqual(st3['defense_streaks'].get(a, 0), 0)
        # old state with only defense_id / defense_streak still seeds that one individual
        old = dict(population=st['population'], nodes=st['nodes'], team=st['team'],
                   defense_id=b, defense_streak=5)
        self.assertEqual(E.defense_streak_for(old, b), 5)
        self.assertEqual(E.defense_streak_for(old, a), 0)

    def test_failing_gate_seat_can_be_replaced_before_tenure_but_accumulating_seats_stay(self):
        ev = EvolutionTests()
        ev.sr, ev.split, ev.obs = self.sr, self.split, self.obs
        ev.addCleanup = lambda *a, **k: None
        ev.setUp()
        ev_size, ev_cap = E.TEAM_SIZE, E.TEAM_DIRECTION_CAP
        E.TEAM_SIZE, E.TEAM_DIRECTION_CAP = 8, 5
        try:
            st, ids = ev._setup([(12 - k, True, 3, 10) for k in range(10)])
            for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % len(G.SPECIES)]
            E.update_hall(st); E.choose_team(st, T0)
            self.assertEqual([m['id'] for m in st['team']], ids[:8])
            # a member still able to finish the 5-gen streak is not bumped, even by a stronger outsider
            st['nodes'][ids[8]]['fitness'] = 99.0
            E.update_hall(st)
            self.assertEqual(E.choose_team(st, T0 + 3600_000), [])
            self.assertEqual([m['id'] for m in st['team']], ids[:8])
            # failing OOS opens the seat immediately, before the 7-day tenure
            st['nodes'][ids[0]]['qualified'] = False; E.update_hall(st)
            changes = E.choose_team(st, T0 + 2 * 3600_000)
            self.assertTrue(changes)
            self.assertEqual(changes[0][1], ids[0])
            self.assertEqual(changes[0][3], 'entry_gate')
            self.assertIn(changes[0][2], ids[8:])
            # too-large drawdown likewise, even if the member is still marked qualified
            st['nodes'][ids[1]]['oos_dd'] = 31.0
            E.update_hall(st)
            changes = E.choose_team(st, T0 + 3 * 3600_000)
            self.assertTrue(any(c[1] == ids[1] and c[3] == 'entry_gate' for c in changes))
        finally:
            E.TEAM_SIZE, E.TEAM_DIRECTION_CAP = ev_size, ev_cap

    def test_update_view_omits_legacy_trades_from_list_and_does_not_drop_stored_legacy(self):
        ev = EvolutionTests()
        ev.sr, ev.split, ev.obs = self.sr, self.split, self.obs
        ev.addCleanup = lambda *a, **k: None
        ev.setUp()
        st, ids = ev._setup([(5.0, True, 3, 10), (4.0, True, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % 4]
        E.update_hall(st); E.choose_team(st, T0)
        ev._grant_entry(st, ids[0])
        P.update(st, self.sr, [])
        marker = dict(inst='BTC-USDT-SWAP', side=1, pnl=-1.25, reason='switch', closed=1, champion='legacy-bot')
        st['paper'].setdefault('legacy', dict(curve=[], trades=[], segments=[]))
        st['paper']['legacy']['trades'] = [marker]
        view, _ = P.update(st, self.sr, [])
        self.assertFalse(any(t.get('champion') == 'legacy-bot' or t.get('closed') == 1 or t.get('reason') == 'switch' for t in view['trades']))
        self.assertEqual(st['paper']['legacy']['trades'][0]['champion'], 'legacy-bot')


class LeverageRiskAndLiquidationTests(unittest.TestCase):
    def test_liquidation_price_long_and_short(self):
        entry, L = 100.0, 20
        long_px = X.liquidation_price(entry, 1, L)
        short_px = X.liquidation_price(entry, -1, L)
        # 1/20 - 0.5% MMR - 0.05% fee = 4.45 %
        self.assertAlmostEqual(long_px, entry * (1 - (1 / L - X.MMR - X.FEE)))
        self.assertAlmostEqual(short_px, entry * (1 + (1 / L - X.MMR - X.FEE)))
        self.assertLess(long_px, entry); self.assertGreater(short_px, entry)
        self.assertAlmostEqual(entry - long_px, short_px - entry)

    def test_clamp_leverage_is_only_a_cap_under_cross_margin(self):
        # cross margin: leverage only sets initial margin, so a wide stop no longer forces a lower L;
        # the only cap is the gene cap LEVERAGE = 20× (or an explicit lower cap)
        self.assertEqual(X.LEVERAGE, 20)
        self.assertEqual(X.clamp_leverage(20, .05), 20)     # isolated would have cut this to < 18×
        self.assertEqual(X.clamp_leverage(20, .02), 20)
        self.assertEqual(X.clamp_leverage(100, .02), 20)    # an old 100× request is clamped to the 20× gene cap
        self.assertEqual(X.clamp_leverage(100, .003), 20)
        self.assertEqual(X.clamp_leverage(250), 20)
        self.assertEqual(X.clamp_leverage(20, .003, cap=10, mmr=X.MMR_OTHER), 10)
        self.assertEqual(X.clamp_leverage(0, .02), 1)
        # the isolated estimate helper is still available for a single-position reference price
        self.assertLess(X.liquidation_price(100.0, 1, 20), 100.0)
        self.assertGreater(X.liquidation_price(100.0, -1, 20), 100.0)

    def test_leverage_gene_never_exceeds_20_after_init_mutation_and_crossover(self):
        rng = random.Random(7)
        seen = set()
        for sp in G.SPECIES:
            for _ in range(200):
                g = G.random_genome(rng, sp)
                self.assertTrue(1 <= g['leverage'] <= 20, g['leverage']); seen.add(g['leverage'])
        self.assertIn(20, seen); self.assertIn(1, seen)   # the whole 1–20 band is reachable
        # a population member saved under the old 1–100× band: heavy mutation always lands back in 1–20
        old = dict(base_genes(), leverage=100)
        for _ in range(500):
            m = G.mutate(old, rng, rate=1.0, scale=1.0)
            self.assertTrue(1 <= m['leverage'] <= 20, m['leverage'])
        # even when the leverage gene itself does not mutate (rate 0), mutate clamps it
        self.assertEqual(G.mutate(old, rng, rate=0.0)['leverage'], 20)
        # crossover of two old high-leverage parents also yields ≤ 20×
        for _ in range(50):
            c = G.crossover(dict(base_genes(), leverage=80), dict(base_genes(), leverage=55), rng)
            self.assertEqual(c['leverage'], 20)

    def test_risk_and_leverage_genes_are_clamped_and_old_genomes_migrate(self):
        self.assertEqual(G.GENES['risk'][1:], (.05, .10))
        self.assertEqual(G.GENES['leverage'][1:], (1, 20))
        low = G.fix(dict(base_genes(), risk=.01, leverage=0))
        self.assertEqual((low['risk'], low['leverage']), (.05, 1))
        self.assertEqual(G.fix(dict(base_genes(), risk=.0499))['risk'], .05)     # a gene from the old 2–10 % band below 5 %
        self.assertEqual(G.fix(dict(base_genes(), risk=.10))['risk'], .10)       # inside both bands: unchanged
        high = G.fix(dict(base_genes(), risk=.25, leverage=250))
        self.assertEqual((high['risk'], high['leverage']), (.10, 20))
        self.assertEqual(G.fix(dict(base_genes(), risk=.12))['risk'], .10)       # a gene from the 5–20 % band above 10 %
        self.assertEqual(G.fix(dict(base_genes(), risk=.20))['risk'], .10)
        self.assertEqual(G.fix(dict(base_genes(), risk=.0999))['risk'], .0999)
        self.assertEqual(G.fix({k: v for k, v in base_genes().items() if k != 'risk'})['risk'], .05)   # missing → 5 %
        rng = random.Random(3)
        for _ in range(200):                                                     # random init and mutation stay inside 5–10 %
            self.assertTrue(.05 <= G.random_genome(rng)['risk'] <= .10)
            self.assertTrue(.05 <= G.mutate(dict(base_genes(), risk=.02), rng, rate=1.0)['risk'] <= .10)
            self.assertTrue(.05 <= G.mutate(dict(base_genes(), risk=.20), rng, rate=1.0)['risk'] <= .10)
            self.assertTrue(.05 <= G.mutate(dict(base_genes(), risk=.20), rng, rate=0.0)['risk'] <= .10)
        # genomes saved under the old 1–100× band are pulled back to ≤ 20× by fix (and so by mutate / crossover)
        self.assertEqual(G.fix(dict(base_genes(), leverage=21))['leverage'], 20)
        self.assertEqual(G.fix(dict(base_genes(), leverage=100))['leverage'], 20)
        self.assertEqual(G.fix(dict(base_genes(), leverage=20))['leverage'], 20)
        missing = {k: v for k, v in base_genes(risk=.07).items() if k != 'leverage'}
        self.assertEqual(G.fix(missing)['leverage'], 10)
        self.assertEqual(G.fix(missing)['risk'], .07)
        # majors ×2 cannot push a 6 % (or an old 12 % / 20 %) gene past 10 %
        self.assertEqual(X.position_risk(.06, 'BTC-USDT-SWAP'), .10)
        self.assertEqual(X.position_risk(.12, 'BTC-USDT-SWAP'), .10)
        self.assertEqual(X.position_risk(.20, 'BTC-USDT-SWAP'), .10)

    def test_cross_liquidation_closes_the_account_at_the_bar_price_with_fees(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 3
        g = base_genes(leverage=20, risk=.10, stop_atr=.8)
        s_open = series_from(closes)
        opened = X.simulate(g, {'BTC-USDT-SWAP': s_open}, 30, s_open.n)['positions']
        self.assertTrue(opened)
        p = opened[0]
        self.assertLessEqual(p['lever'], 20)
        # crash far enough that mark equity <= maintenance margin (cross, not the isolated liq estimate)
        crash = series_from(closes + [1.0], opens=[closes[0]] + closes)
        crash.l[-1] = crash.o[-1] = 1.0; crash.h[-1] = 1.0; crash._cache.clear()
        r = X.simulate(g, {'BTC-USDT-SWAP': crash}, 30, crash.n)
        liq = [t for t in r['trades'] if t['reason'] == 'liquidation']
        self.assertTrue(liq)
        t = liq[-1]
        # closed at the bar price with the taker fee (not an isolated-margin wipe)
        self.assertAlmostEqual(t['exit'], 1.0, places=6)
        fee = t['qty'] * t['exit'] * X.FEE
        entry_fee = p['qty'] * p['entry'] * X.FEE
        want = t['qty'] * (t['exit'] - p['entry']) * p['side'] - fee - entry_fee - (p.get('funding') or 0)
        self.assertAlmostEqual(t['pnl'], want, places=2)
        self.assertEqual(r['positions'], [])
        # funding is notional × rate, not margin × rate, and is charged to shared cash
        s2 = series_from([100.0] * 40 + [110.0] + [111.0] * 16)
        s2.funding = {t: .001 for t in s2.ts if t % (8 * H) == 0}
        r2 = X.simulate(base_genes(leverage=25, risk=.02), {'BTC-USDT-SWAP': s2}, 30, s2.n)
        p2 = r2['positions'][0]
        paid = sum(p2['qty'] * s2.o[i] * .001 for i in range(s2.n) if s2.ts[i] in s2.funding and s2.ts[i] > p2['opened'])
        self.assertGreater(paid, 0)
        self.assertAlmostEqual(p2['funding'], round(paid, 4), places=3)
        self.assertNotAlmostEqual(p2['funding'], p2['margin'] * .001, places=3)

    def test_shared_account_cross_liquidation_closes_every_position(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 3
        sr = {k: series_from(list(closes), k) for k in ('C0-USDT-SWAP', 'C1-USDT-SWAP')}
        g = base_genes(risk=.10, leverage=20, stop_atr=.8)
        r0 = X.simulate_account([dict(id='m', genes=g, from_i=0, wind_i=None, boosts=[])], sr, 30, len(closes), equity=10000.0)
        self.assertEqual(len(r0['positions']), 2)
        # crash C0 far enough that the *account* is under total MM; C1 has not moved, but both must close
        for s in sr.values():
            s.o.append(s.c[-1]); s.h.append(s.c[-1]); s.l.append(s.c[-1]); s.c.append(s.c[-1]); s.ts.append(s.ts[-1] + H); s.n += 1
        sr['C0-USDT-SWAP'].o[-1] = 1.0
        sr['C0-USDT-SWAP'].l[-1] = 1.0
        sr['C0-USDT-SWAP'].h[-1] = 1.0
        sr['C0-USDT-SWAP'].c[-1] = 1.0
        for s in sr.values():
            s._cache.clear()
        r1 = X.simulate_account([dict(id='m', genes=g, from_i=0, wind_i=None, boosts=[])], sr, 30, sr['C0-USDT-SWAP'].n, equity=10000.0)
        liq = [t for t in r1['trades'] if t['reason'] == 'liquidation']
        self.assertEqual(sorted(t['inst'] for t in liq), ['C0-USDT-SWAP', 'C1-USDT-SWAP'])
        self.assertEqual(r1['positions'], [])
        self.assertFalse(any(t['reason'] == 'equity' for t in r1['trades']))
        c0 = next(t for t in liq if t['inst'] == 'C0-USDT-SWAP')
        self.assertAlmostEqual(c0['exit'], 1.0, places=6)

    def test_mmr_tiers_and_instrument_leverage_caps(self):
        self.assertEqual(X.maintenance_rate('BTC-USDT-SWAP'), .005)
        self.assertEqual(X.maintenance_rate('ETH-USDT-SWAP'), .005)
        self.assertEqual(X.maintenance_rate('SOL-USDT-SWAP'), .005)
        self.assertEqual(X.maintenance_rate('BNB-USDT-SWAP'), .005)
        self.assertEqual(X.maintenance_rate('XRP-USDT-SWAP'), .005)
        self.assertEqual(X.maintenance_rate('DOGE-USDT-SWAP'), .005)
        self.assertEqual(X.maintenance_rate('PEPE-USDT-SWAP'), .025)
        self.assertEqual(X.maintenance_rate('C0-USDT-SWAP'), .025)
        # the default cap is the 20× gene cap; the snapshot still matters below it and with an explicit cap
        self.assertEqual(X.inst_max_leverage('BTC-USDT-SWAP'), 20)
        self.assertEqual(X.inst_max_leverage('BTC-USDT-SWAP', cap=100), 100)
        self.assertEqual(X.inst_max_leverage('BNB-USDT-SWAP', cap=100), 50)  # OKX snapshot, not forced to 100
        self.assertEqual(X.inst_max_leverage('C0-USDT-SWAP'), 20)           # unknown alt → 20×
        self.assertEqual(X.FALLBACK_INST_LEVERAGE, 20)
        # a major missing from the snapshot also falls back to 20× (same as the OKX demo), not 100×
        saved = X.INST_MAX_LEVERAGE.pop('BTC-USDT-SWAP', None)
        try:
            self.assertEqual(X.inst_max_leverage('BTC-USDT-SWAP'), 20)
        finally:
            if saved is not None:
                X.INST_MAX_LEVERAGE['BTC-USDT-SWAP'] = saved
        # under cross margin clamp_leverage does not cut 20× because of the 2.5 % MMR; the per-coin cap still does
        self.assertEqual(X.clamp_leverage(20, .003, mmr=X.MMR_OTHER), 20)
        self.assertEqual(X.clamp_leverage(100, .003, mmr=X.MMR_OTHER), 20)
        self.assertEqual(X.inst_max_leverage('C0-USDT-SWAP', cap=100), 20)

    def test_paper_entry_uses_inst_cap_and_alt_mmr(self):
        # unknown alt, gene 100×, tight stop: cap 20× (fallback) and 2.5 % MMR still leaves room at 20×
        closes = [100.0] * 40 + [110.0] + [111.0] * 3
        s = series_from(closes, 'C0-USDT-SWAP')
        g = base_genes(leverage=100, risk=.02, stop_atr=.8)
        r = X.simulate(g, {'C0-USDT-SWAP': s}, 30, s.n)
        self.assertTrue(r['positions'])
        p = r['positions'][0]
        self.assertEqual(p['lever'], 20)
        self.assertAlmostEqual(p['liq'], X.liquidation_price(p['entry'], 1, 20, mmr=.025), places=6)
        # BTC allows 100× on OKX and the stop fits under 0.5 % MMR, but the gene cap still limits it to 20×
        s_btc = series_from(closes, 'BTC-USDT-SWAP')
        r2 = X.simulate(g, {'BTC-USDT-SWAP': s_btc}, 30, s_btc.n)
        self.assertTrue(r2['positions'])
        self.assertEqual(r2['positions'][0]['lever'], 20)

    @STOP_RISK
    def test_funding_is_charged_to_shared_cash_and_can_trigger_cross_liquidation(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 16
        s = series_from(closes, 'BTC-USDT-SWAP')
        s.funding = {t: .01 for t in s.ts if t % (8 * H) == 0}
        r = X.simulate(base_genes(leverage=10, risk=.02), {'BTC-USDT-SWAP': s}, 30, s.n)
        self.assertTrue(r['positions'])
        p = r['positions'][0]
        self.assertGreater(p['funding'], 0)
        # informational isolated-style estimate still tracks remaining margin after funding
        remaining = p['margin'] - p['funding']
        want = X.liquidation_price(p['entry'], 1, p['lever'], mmr=X.maintenance_rate('BTC-USDT-SWAP'),
                                   margin=remaining, qty=p['qty'])
        self.assertAlmostEqual(p['liq'], want, places=6)

        # two alts open; a huge funding payment on C0 takes shared cash below total MM → both close as liquidation
        closes = [100.0] * 40 + [110.0] + [111.0] * 3
        sr = {k: series_from(list(closes), k) for k in ('C0-USDT-SWAP', 'C1-USDT-SWAP')}
        g = base_genes(risk=.02, leverage=10)
        r0 = X.simulate_account([dict(id='m', genes=g, from_i=0, wind_i=None, boosts=[])], sr, 30, len(closes), equity=10000.0)
        self.assertEqual(len(r0['positions']), 2)
        for s in sr.values():
            s.o.append(s.c[-1]); s.h.append(s.c[-1]); s.l.append(s.c[-1]); s.c.append(s.c[-1]); s.ts.append(s.ts[-1] + H); s.n += 1
            s._cache.clear()
        last_ts = sr['C0-USDT-SWAP'].ts[-1]
        sr['C0-USDT-SWAP'].funding = {last_ts: 50.0}   # test-only rate: pay 50× notional from shared cash
        r1 = X.simulate_account([dict(id='m', genes=g, from_i=0, wind_i=None, boosts=[])], sr, 30, sr['C0-USDT-SWAP'].n, equity=10000.0)
        liq = [t for t in r1['trades'] if t['reason'] == 'liquidation']
        self.assertEqual(sorted(t['inst'] for t in liq), ['C0-USDT-SWAP', 'C1-USDT-SWAP'])
        self.assertFalse(any(t['reason'] == 'equity' for t in r1['trades']))
        self.assertEqual(r1['positions'], [])
        self.assertEqual(r1['liquidations'], 1)                     # one account event, two closed positions
        self.assertGreater(sum(t['funding'] for t in liq if t['inst'] == 'C0-USDT-SWAP'), 0)
        self.assertGreaterEqual(r1['equity'], 0.0)

    def test_cross_open_allowed_rejects_a_stop_that_would_breach_maintenance_margin(self):
        # a lone position whose stop would leave no cash must be skipped
        wipe = dict(side=1, entry=100.0, qty=200.0, stop=1.0, fee=10.0, funding=0.0, lever=20, margin=1000.0, mmr=0.025)
        ok, why = X.cross_open_allowed(10000.0, {}, 'C0-USDT-SWAP', wipe, {})
        self.assertFalse(ok)
        self.assertEqual(why, 'stop_before_liquidation')
        # already-open book with a large maintenance margin: a wide new stop is rejected
        existing = {'C0-USDT-SWAP': dict(side=1, entry=100.0, qty=3200.0, stop=99.5, fee=160.0, funding=0.0,
                                         lever=20, margin=16000.0, mmr=0.025)}
        prices = {'C0-USDT-SWAP': 100.0}
        cash = 10000.0 - 160.0
        wide = dict(side=1, entry=100.0, qty=80.0, stop=50.0, fee=4.0, funding=0.0, lever=20, margin=400.0, mmr=0.025)
        ok, why = X.cross_open_allowed(cash, existing, 'C1-USDT-SWAP', wide, prices)
        self.assertFalse(ok)
        self.assertIn(why, ('stop_before_liquidation', 'equity_below_mm'))
        # a tight, small add-on on a modest book is allowed
        modest = {'C0-USDT-SWAP': dict(side=1, entry=100.0, qty=10.0, stop=99.0, fee=0.5, funding=0.0,
                                       lever=20, margin=50.0, mmr=0.025)}
        tight = dict(side=1, entry=100.0, qty=1.0, stop=99.0, fee=0.05, funding=0.0, lever=20, margin=5.0, mmr=0.025)
        ok2, why2 = X.cross_open_allowed(10000.0 - 0.5, modest, 'C1-USDT-SWAP', tight, {'C0-USDT-SWAP': 100.0})
        self.assertTrue(ok2, msg=why2)
        self.assertIsNone(why2)
        # a position with no managed stop (a manual one on the OKX demo account) keeps its maintenance margin in every
        # scenario: 9,000 USDT of MM leaves too little room for this stop, although the same entry alone is fine
        manual = {'M-USDT-SWAP#long#manual': dict(side=1, entry=1.0, qty=360000.0, mmr=0.025)}
        mid = dict(side=1, entry=100.0, qty=100.0, stop=90.0, fee=5.0, funding=0.0, mmr=0.025)
        self.assertEqual(X.cross_open_allowed(10000.0, {}, 'C1-USDT-SWAP', mid, {})[0], True)
        ok3, why3 = X.cross_open_allowed(10000.0, {}, 'C1-USDT-SWAP', mid, {'M-USDT-SWAP#long#manual': 1.0}, fixed=manual)
        self.assertEqual((ok3, why3), (False, 'stop_before_liquidation'))
        # every stop at once: two open longs whose stops together nearly empty the account block a third
        book = {k: dict(side=1, entry=100.0, qty=45.0, stop=0.5, fee=0.0, funding=0.0, mmr=0.005) for k in ('A', 'B')}
        third = dict(side=1, entry=100.0, qty=30.0, stop=60.0, fee=1.5, funding=0.0, mmr=0.005)
        ok4, why4 = X.cross_open_allowed(10000.0, book, 'C', third, {'A': 100.0, 'B': 100.0})
        self.assertEqual((ok4, why4), (False, 'stop_before_liquidation'))


class CrossReviewFixTests(unittest.TestCase):
    """PR #88 review: stop-gate MM before the stop fills, same-bar event order, bankruptcy accounting.

    These are engine-mechanics tests. Under the 20× gene cap (PR #90) a lone 10 % risk position on a 0.26 % stop does not
    fit the free margin, and no single 10 % risk position can be liquidated before its stop at all, so the ordering /
    bankruptcy paths would never be reached. The leverage cap is therefore raised to 100× inside this class only; the
    production cap stays execution.LEVERAGE = 20 (see test_the_20x_cap_keeps_these_entries_out).
    """
    ZEC = 'ZEC-USDT-SWAP'

    def setUp(self):
        self.raised = [mock.patch.object(X, 'LEVERAGE', 100), mock.patch.dict(X.ACCOUNT_RULES, lever=100, leader_lever=100, sizing='stop_risk', **PRIOR_ERA)]   # stop-risk engine
        for p in self.raised:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.raised])

    def test_the_20x_cap_keeps_these_entries_out(self):
        for p in self.raised:
            p.stop()
        self.raised.clear()
        pin = mock.patch.dict(X.ACCOUNT_RULES, sizing='stop_risk')   # stop-risk sizing (the 20× cap mechanics of that rule)
        pin.start(); self.addCleanup(pin.stop)
        self.assertEqual((X.LEVERAGE, X.ACCOUNT_RULES['lever'], X.ACCOUNT_RULES['leader_lever']), (20, 20, 20))
        # at 20× the ZEC scenario needs ~1.9 × equity of margin, so even with the stop gate bypassed nothing opens
        acc, one = self.run_both(self.ZEC, self.bars(self.ZEC, 99.7), self.genes())
        self.assertEqual((acc['trades'], acc['positions'], acc['liquidations']), ([], [], 0))
        self.assertEqual((one['trades'], one['positions']), ([], []))

    def bars(self, inst, low, opens=100.0, high=100.1, close=100.0, n=3):
        ts = [T0 + i * H for i in range(n)]
        return X.Series(inst, ts, [100.0] * (n - 1) + [opens], [100.1] * (n - 1) + [high],
                        [99.99] * (n - 1) + [low], [100.0] * (n - 1) + [close])

    def genes(self, **kw):
        return dict(dict(risk=.1, leverage=100, target_rr=0, max_hold=240, tf=1, trail=0), **kw)

    def run_both(self, inst, s, g, gate=False):
        """(simulate_account result, simulate result) with one long signal on bar 0 (stop 0.26 below the fill)."""
        ev = {0: [(inst, 1, .26)]}
        patches = [mock.patch.object(X, 'signal_sets', return_value=(ev, {}))]
        if not gate:
            patches.append(mock.patch.object(X, 'cross_open_allowed', return_value=(True, None)))
        for p in patches: p.start()
        try:
            acc = X.simulate_account([dict(id='m', genes=g, from_i=0)], {inst: s}, 0, s.n)
            one = X.simulate(g, {inst: s}, 0, s.n, events=ev)
        finally:
            for p in patches: p.stop()
        return acc, one

    def zec_position(self):
        p = dict(side=1, entry=100.02, qty=10000.0 * .1 / .26, stop=100.02 - .26, mmr=X.maintenance_rate(self.ZEC), funding=0.0)
        p['fee'] = p['qty'] * p['entry'] * X.FEE
        return p

    # ---- 1) the stop gate keeps every position's MM until the stop has filled
    def test_gate_rejects_an_entry_liquidated_before_its_stop_price(self):
        p = self.zec_position()
        self.assertEqual(X.cross_open_allowed(10000.0, {}, self.ZEC, p, {}), (False, 'stop_before_liquidation'))
        cash = 10000.0 - p['fee']
        self.assertTrue(X.book_liquidated(cash, {self.ZEC: p}, {self.ZEC: p['stop']}))     # the reviewer's case: dead at the stop
        # the account engine therefore never opens it
        acc, one = self.run_both(self.ZEC, self.bars(self.ZEC, 99.7), self.genes(), gate=True)
        self.assertEqual((acc['trades'], acc['positions'], acc['liquidations']), ([], [], 0))
        self.assertEqual((one['trades'], one['positions']), ([], []))

    def test_gate_counts_every_open_stop_before_any_fills(self):
        mk = lambda entry, stop, qty: dict(side=1, entry=entry, qty=qty, stop=stop, fee=0.0, funding=0.0, mmr=.005)
        book = {'A': mk(100.0, 98.5, 2000.0), 'B': mk(100.0, 98.5, 2000.0)}
        new = dict(mk(100.0, 98.0, 1000.0), fee=50.0)
        # C's stop alone is fine and the cash left after every stop is positive (the old rules let it in), but on the way —
        # A, B and C all at their stops, none filled yet — equity 1,950 is below the 2,460 MM of the whole book
        self.assertEqual(X.cross_open_allowed(10000.0, book, 'C', new, {'A': 100.0, 'B': 100.0}), (False, 'stop_before_liquidation'))
        self.assertEqual(X.cross_open_allowed(10000.0, book, 'C', dict(new, qty=50.0, fee=2.5), {'A': 100.0, 'B': 100.0}), (True, None))

    def test_an_allowed_entry_survives_every_stop_order(self):
        rng = random.Random(7)
        checked = 0
        for _ in range(400):
            n = rng.randint(0, 2)
            book = {}
            for k in range(n):
                e = rng.uniform(50, 150); side = rng.choice((1, -1))
                book[f'K{k}'] = dict(side=side, entry=e, qty=rng.uniform(10, 600), stop=e * (1 - side * rng.uniform(.003, .05)),
                                     fee=0.0, funding=0.0, mmr=rng.choice((.005, .025)))
            prices = {k: p['entry'] * (1 + rng.uniform(-.002, .002)) for k, p in book.items()}
            e = rng.uniform(50, 150); side = rng.choice((1, -1)); qty = rng.uniform(10, 600)
            new = dict(side=side, entry=e, qty=qty, stop=e * (1 - side * rng.uniform(.003, .05)), fee=qty * e * X.FEE, funding=0.0,
                       mmr=rng.choice((.005, .025)))
            cash = 10000.0 - sum(p['qty'] * (prices[k] - p['entry']) * p['side'] for k, p in book.items())
            if not X.cross_open_allowed(cash, book, 'N', new, prices)[0]:
                continue
            checked += 1
            # walk every position to its stop, filling the stops one by one in a random order; never liquidated
            pos = dict(book, N=new); cash -= new['fee']
            px = {k: p['stop'] for k, p in pos.items()}
            order = list(pos); rng.shuffle(order)
            self.assertFalse(X.book_liquidated(cash, pos, px))
            for k in order:
                _, gain, _ = X.close_proceeds(pos.pop(k), px.pop(k), slip=True)
                cash += gain
                self.assertFalse(X.book_liquidated(cash, pos, px))
            self.assertGreater(cash, 0)
        self.assertGreater(checked, 20)

    # ---- 2) same-bar order: an intrabar liquidation comes before stop / target / time exits
    def test_intrabar_liquidation_before_the_stop_is_counted_in_both_engines(self):
        acc, one = self.run_both(self.ZEC, self.bars(self.ZEC, 99.7), self.genes())
        for r in (acc, one):
            self.assertEqual([t['reason'] for t in r['trades']], ['liquidation'])
            self.assertEqual(r['liquidations'], 1)
            self.assertEqual(r['trades'][0]['exit'], 100.02 - .26)        # at the stop price, where the breach is first seen

    def test_time_exit_at_the_close_does_not_hide_an_intrabar_liquidation(self):
        acc, one = self.run_both(self.ZEC, self.bars(self.ZEC, 99.9), self.genes(max_hold=1))
        for r in (acc, one):
            self.assertEqual([t['reason'] for t in r['trades']], ['liquidation'])
            self.assertEqual(r['liquidations'], 1)
            self.assertEqual(r['trades'][0]['exit'], 99.9)

    def test_target_in_the_same_bar_does_not_hide_an_intrabar_liquidation(self):
        acc, one = self.run_both(self.ZEC, self.bars(self.ZEC, 99.9, high=100.3), self.genes(target_rr=1))
        for r in (acc, one):
            self.assertEqual([t['reason'] for t in r['trades']], ['liquidation'])
            self.assertEqual(r['liquidations'], 1)

    def test_a_stop_that_comes_first_is_not_turned_into_a_liquidation(self):
        # BTC (0.5 % MMR): fine at its stop, but the bar's low far beyond it would breach MM — the stop fills first
        k = 'BTC-USDT-SWAP'
        acc, one = self.run_both(k, self.bars(k, 95.0, close=96.0), self.genes(), gate=True)
        for r in (acc, one):
            self.assertEqual([t['reason'] for t in r['trades']], ['stop'])
            self.assertEqual(r['liquidations'], 0)
        # without the liquidation the position simply stays open (no time exit any more)
        acc, one = self.run_both(k, self.bars(k, 99.9), self.genes(max_hold=1), gate=True)
        self.assertEqual((acc['trades'], one['trades']), ([], []))
        self.assertEqual((len(acc['positions']), len(one['positions'])), (1, 1))

    def test_a_gap_through_the_stop_still_fills_at_the_open(self):
        k = 'BTC-USDT-SWAP'
        acc, one = self.run_both(k, self.bars(k, 99.5, opens=99.6, high=99.7, close=99.6), self.genes(), gate=True)
        for r in (acc, one):
            self.assertEqual([t['reason'] for t in r['trades']], ['stop'])
            self.assertAlmostEqual(r['trades'][0]['exit'], 99.6 * (1 - X.SLIP))

    # ---- 3) bankruptcy: cash floored at 0 with an explicit insurance adjustment that reconciles
    def test_gap_past_bankruptcy_records_insurance_and_reconciles(self):
        s = self.bars(self.ZEC, 1.0, opens=1.0, high=1.0, close=1.0)
        acc, one = self.run_both(self.ZEC, s, self.genes())
        for r in (acc, one):
            self.assertEqual((r['cash'], r['liquidations'], r['positions']), (0.0, 1, []))
            self.assertGreater(r['insurance'], 0)
            self.assertAlmostEqual(10000.0 + sum(t['pnl'] for t in r['trades']) + r['insurance'], r['cash'], places=6)
        self.assertAlmostEqual(10000.0 + sum(acc['pnl'].values()) + acc['insurance'], acc['equity'], places=6)
        self.assertEqual([(a['kind'], a['ts']) for a in acc['adjustments']], [('insurance', s.ts[-1])])
        self.assertAlmostEqual(acc['adjustments'][0]['amount'], acc['insurance'], places=9)
        # an ordinary liquidation (cash stays positive) needs no adjustment
        acc, one = self.run_both(self.ZEC, self.bars(self.ZEC, 99.7), self.genes())
        self.assertEqual((acc['insurance'], acc['adjustments'], one['insurance']), (0.0, [], 0.0))
        self.assertAlmostEqual(10000.0 + sum(t['pnl'] for t in acc['trades']), acc['cash'], places=6)


class EntryGatePaperIntegrationTests(unittest.TestCase):
    def test_gate_streak_team_selection_and_paper_step(self):
        ev = EvolutionTests()
        ev.sr = tiny_market(); ev.split = D.split(ev.sr); ev.obs = D.observe_volatility(ev.sr)
        ev.addCleanup = lambda *a, **k: None
        ev.setUp()
        ev_size, ev_cap = E.TEAM_SIZE, E.TEAM_DIRECTION_CAP
        E.TEAM_SIZE, E.TEAM_DIRECTION_CAP = 8, 5
        try:
            st, ids = ev._setup([(12 - k, True, 3, 10) for k in range(10)])
            for k, nid in enumerate(ids):
                st['nodes'][nid]['species'] = G.SPECIES[k % len(G.SPECIES)]
                st['nodes'][nid]['genes'] = G.fix(st['nodes'][nid]['genes'])
            E.update_hall(st); E.choose_team(st, T0)
            self.assertEqual([m['id'] for m in st['team']], ids[:8])
            # no 5-gen streak yet: paper winds every seat and holds cash
            view, _ = P.update(st, ev.sr, [])
            self.assertFalse(any(g['wind_ts'] is None for g in st['paper']['account']['segs']))
            self.assertEqual(view['pending'], [])
            # five new generations accumulate a per-seat streak (migrate + increment once each)
            for _ in range(5):
                E.record_defense(st)
            self.assertEqual(st['defense_streaks'][ids[0]], 5)
            self.assertTrue(E.entry_gate(st['nodes'][ids[0]], state=st)['passed'])
            # a seat that fails OOS is replaced immediately, bypassing tenure
            st['nodes'][ids[0]]['qualified'] = False; E.update_hall(st)
            changes = E.choose_team(st, T0 + 3600_000)
            self.assertTrue(any(c[1] == ids[0] and c[3] == 'entry_gate' for c in changes))
            new_id = next(c[2] for c in changes if c[1] == ids[0] and c[3] == 'entry_gate')
            self.assertLess(E.defense_streak_for(st, new_id), 5)
            # do not call _grant_entry for the new seat: streak < 5 means it must wind down, no new entries
            for m in st['team']:
                if m['id'] != new_id:
                    ev._grant_entry(st, m['id'])
            view2, signals = P.update(st, ev.sr, changes)
            live = [g for g in st['paper']['account']['segs'] if g['wind_ts'] is None]
            self.assertTrue(live)
            self.assertNotIn(new_id, [g['champion'] for g in live])
            self.assertFalse(any(g['champion'] == new_id and g['wind_ts'] is None for g in st['paper']['account']['segs']))
            self.assertFalse(any(m.get('id') == new_id and m.get('trading') for m in view2['members']))
            self.assertFalse(any(sig.get('champion') == new_id for sig in signals.values()))
            self.assertTrue(view2['members'])
            self.assertEqual(view2['rules']['max_risk'], .10)
            self.assertEqual(view2['rules']['lever'], 20)
            if view2['leader']:
                self.assertLessEqual(view2['leader']['risk'], .10)
                self.assertLessEqual(view2['leader']['leverage'], 20)
            for sig in signals.values():
                self.assertLessEqual(sig['risk'], .10)
                self.assertGreaterEqual(sig['risk'], .05)
                self.assertLessEqual(sig['leverage'], 20)
        finally:
            E.TEAM_SIZE, E.TEAM_DIRECTION_CAP = ev_size, ev_cap

    def test_migrated_streak_increments_exactly_once_on_the_next_generation(self):
        """Off-by-one guard: seeding from defense_streak then record_defense adds one, not zero or two."""
        ev = EvolutionTests()
        ev.sr = tiny_market(); ev.split = D.split(ev.sr)
        ev.addCleanup = lambda *a, **k: None
        ev.setUp()
        st, ids = ev._setup([(5.0, True, 3, 10), (4.0, True, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % 4]
        E.update_hall(st); E.choose_team(st, T0)
        a, b = st['team'][0]['id'], st['team'][1]['id']
        old = dict(population=st['population'], nodes=st['nodes'], team=st['team'],
                   defense_id=b, defense_streak=5)
        self.assertEqual(E.defense_streak_for(old, b), 5)          # seed only, no increment
        self.assertEqual(E.defense_streak_for(old, a), 0)
        E.record_defense(old)                                      # first new generation after the upgrade
        self.assertEqual(old['defense_streaks'][b], 6)   # seeded 5, plus this generation
        self.assertEqual(old['defense_streaks'][a], 1)


class UniverseTests(unittest.TestCase):

    def test_top_volume_crypto_perpetuals_listed_for_the_whole_window(self):
        now, day = T0 + 400 * 24 * H, 24 * H
        inst = lambda i, listed, cat='1', settle='USDT', state='live': dict(instId=i, listTime=str(now - listed * day), instCategory=cat, settleCcy=settle, state=state)
        meta = [inst('BTC-USDT-SWAP', 2000), inst('ETH-USDT-SWAP', 2000), inst('NEW-USDT-SWAP', 100), inst('AAPL-USDT-SWAP', 900, cat='3'),
                inst('BTC-USD-SWAP', 2000, settle='BTC'), inst('OLD-USDT-SWAP', 900, state='suspend'), inst('PEPE-USDT-SWAP', 800)]
        tick = [dict(instId=i, last=p, volCcy24h=v) for i, p, v in (('BTC-USDT-SWAP', '1', '1'), ('ETH-USDT-SWAP', '1', '50'), ('NEW-USDT-SWAP', '1', '99'),
                                                                     ('AAPL-USDT-SWAP', '1', '98'), ('OLD-USDT-SWAP', '1', '97'), ('PEPE-USDT-SWAP', '2', '40'))]
        get = lambda path: meta if path.startswith('public/instruments') else tick
        self.assertEqual(D.select_universe(now, get, size=3), ['BTC-USDT-SWAP', 'PEPE-USDT-SWAP', 'ETH-USDT-SWAP'])
        with tempfile.TemporaryDirectory() as tmp:
            first = D.universe(tmp, now, get)
            self.assertEqual(D.universe(tmp, now, lambda p: []), first)          # chosen once, then fixed

    def test_universe_is_the_top_50_and_a_longer_saved_list_is_cut_in_its_own_order(self):
        self.assertEqual(D.UNIVERSE_SIZE, 50)
        coins = [f'C{k}-USDT-SWAP' for k in range(86)]                      # the 86-coin list chosen under the old top-100 rule
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'evolve' / 'universe.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(dict(version=D.UNIVERSE_VERSION, chosen_at=1, rule='top 100', instruments=coins,
                                            specs={c: [1, 1, 1] for c in coins}, price_off={'C80-USDT-SWAP': [[1, None]]})))
            got, specs = D.universe(tmp, 5, lambda p: [], with_specs=True)
            self.assertEqual(got, coins[:50])                                   # same order, first 50, no re-ranking
            self.assertEqual(set(specs), set(coins[:50]))
            saved = json.loads(path.read_text())
            self.assertEqual((saved['instruments'], saved['rule'][:6], saved['chosen_at']), (coins[:50], 'top 50', 1))
            self.assertEqual(D.universe(tmp, 6, lambda p: []), coins[:50])     # stays at 50 afterwards


class DataTests(unittest.TestCase):
    def fake(self, now, hours):
        rows = [[str(now - (k + 1) * H), '1', '1.1', '.9', '1', '0', '0', '0', '1'] for k in range(hours)]   # newest first
        calls = []
        def get(path):
            calls.append(path)
            if path.startswith('market/candles'):
                return [[str(now), '1', '1', '1', '1', '0', '0', '0', '0']] + rows[:300]          # first row still open
            after = int(path.split('after=')[1].split('&')[0])
            return [r for r in rows if int(r[0]) < after][:100]
        return get, calls

    def test_backfill_then_incremental_only_closed_bars_trimmed_to_window(self):
        now = T0 + 400 * 24 * H
        get, calls = self.fake(now, 380 * 24)
        with tempfile.TemporaryDirectory() as tmp:
            rows = D.update_candles(tmp, 'BTC-USDT-SWAP', now, get)
            self.assertEqual(rows[-1][0], now - H)                  # the open bar is excluded
            self.assertGreaterEqual(rows[0][0], now - D.WINDOW_DAYS * 24 * H)
            self.assertEqual(len(rows), len({r[0] for r in rows}))
            n_calls = len(calls)
            get2, calls2 = self.fake(now + 2 * H, 380 * 24 + 2)
            rows2 = D.update_candles(tmp, 'BTC-USDT-SWAP', now + 2 * H, get2)
            self.assertEqual(rows2[-1][0], now + H); self.assertEqual(len(calls2), 1)   # no history paging needed
            self.assertGreater(n_calls, 10)

    def test_split_is_train_test_final_after_warmup(self):
        sr = tiny_market(1250)
        start, cut, mid, end = D.split(sr)
        self.assertEqual(start, 125); self.assertEqual(cut - start, int((end - start) * .6)); self.assertEqual(mid - cut, int((end - start) * .2))
        self.assertGreater(end, mid)


class RunTests(unittest.TestCase):
    def test_end_to_end_offline_writes_dashboard_and_mirror_signals(self):
        now = T0 + 60 * 24 * H
        rngs = {}
        def get(path):
            inst = path.split('instId=')[1].split('&')[0]
            rng = rngs.setdefault(inst, random.Random(inst))
            if path.startswith('market/candles'):
                after = now
            else:
                after = int(path.split('after=')[1].split('&')[0])
            out, x = [], 100 + rng.random()
            for k in range(100 if 'history' in path else 300):
                ts = after - (k + 1) * H
                if ts < now - 50 * 24 * H: break
                x *= math.exp(random.Random(f'{inst}{ts}').gauss(0, .012))
                out.append([str(ts), str(x), str(x * 1.006), str(x * .994), str(x * 1.001), '0', '0', '0', '1'])
            return out
        D.okx_get = get
        import data as dmod
        orig = dmod.okx_get
        try:
            dmod.load_series.__defaults__ = (get, D.INSTRUMENTS, None)
            clock = R.time.time
            R.time.time = lambda: 1794300021.0            # fixed clock: the population seed comes from it, keep the run reproducible
            self.addCleanup(setattr, R.time, 'time', clock)
            with tempfile.TemporaryDirectory() as tmp:
                R.main(['--data', tmp, '--generations', '2'])
                out = Path(tmp) / 'evolve'
                dash = json.loads((out / 'dashboard.json').read_text())
                self.assertEqual(dash['generation'], 2); self.assertEqual(len(dash['history']), 2)
                self.assertTrue(dash['nodes']); self.assertEqual(dash['data']['train_share'], .6); self.assertIn('final_from', dash['data'])
                # Lineage nodes are slim; seated members keep their genes; every node's details are in nodes.json.
                seated = {m['id'] for m in dash['team']} | ({dash['champion']['id']} if dash.get('champion') else set())
                self.assertEqual(dash['schema'], 1); self.assertEqual(dash['details'], 'nodes.json')   # schema 1: pages from before nodes.json still read it
                self.assertEqual(dash['gene_bounds']['risk'], [0.05, 0.10])
                self.assertEqual(dash['gene_bounds']['leverage'], [1, 20])
                for n in dash['nodes']: self.assertIn('genes', n); self.assertEqual('summary' in n, n['id'] in seated); self.assertIn('train', n)
                det = json.loads((out / 'nodes.json').read_text())
                self.assertEqual(det['generation'], 2); self.assertEqual(set(det['nodes']), {n['id'] for n in dash['nodes']})
                self.assertTrue(all({'genes', 'summary', 'test_curve'} <= set(v) for v in det['nodes'].values()))
                sig = json.loads((out / 'signals.json').read_text())
                self.assertIn('positions', sig)
                for inst, p in sig['positions'].items():
                    self.assertIn(p['side'], (1, -1)); self.assertIn('stop', p); self.assertIn('opened', p)
                # NEXT-HOUR RULE: the run stamps its changes on floor(now, 1 h) + 1 h of its own clock
                eff = P.effective_bar(1794300021000)
                self.assertEqual(eff, 1794301200000)
                self.assertEqual(dash['timing']['effective_bar'], R.iso(eff))
                acc = json.loads((out / 'state.json').read_text())['paper']['account']
                self.assertEqual(acc['next_hour_from'], eff)
                self.assertTrue(all(g['from_ts'] in (eff, acc['from_ts']) for g in acc['segs']))
                R.main(['--data', tmp, '--generations', '1'])            # second run resumes the same population
                self.assertEqual(json.loads((out / 'dashboard.json').read_text())['generation'], 3)
        finally:
            dmod.load_series.__defaults__ = (orig, None, None)


if __name__ == '__main__':
    unittest.main()


class ExitRuleTests(unittest.TestCase):
    """2026-11-21 exits, the same for every strategy: stop + take-profit, reverse signal, trailing stop, no time exit."""
    INST = 'BTC-USDT-SWAP'

    def flat(self, n=12):
        ts = [T0 + i * H for i in range(n)]
        return X.Series(self.INST, ts, [100.0] * n, [100.1] * n, [99.99] * n, [100.0] * n)

    def genes(self, name, **kw):
        return dict(dict(name=name, risk=.05, leverage=10, target_rr=0, max_hold=240, tf=1, trail=0), **kw)

    def account(self, sets, segs, s, **kw):
        with mock.patch.object(X, 'signal_sets', side_effect=lambda g, series: sets[g['name']]):
            return X.simulate_account(segs, {self.INST: s}, 0, s.n, **kw)

    # ---- 1) take-profit on every position
    def test_target_rr_range_excludes_zero_and_fix_maps_old_values(self):
        self.assertEqual(G.GENES['target_rr'], ('float', 1.0, 6.0))
        self.assertEqual((G.DEFAULT_TARGET_RR, X.DEFAULT_TARGET_RR), (3.0, 3.0))
        for stored, want in ((0.0, 3.0), (0.3, 3.0), (None, 3.0), (0.5, 1.0), (0.8, 1.0), (2.37, 2.37), (6.0, 6.0), (9.0, 6.0)):
            g = base_genes(); g['target_rr'] = stored
            if stored is None:
                del g['target_rr']
            self.assertEqual(G.fix(g)['target_rr'], want, stored)
            self.assertEqual(X.target_rr({'target_rr': stored}), want, stored)
        rng = random.Random(3)
        for _ in range(300):                                          # new genomes stay inside [1, 6]; a step below 1R is 1R, not 3R
            g = G.mutate(G.random_genome(rng), rng, rate=1.0, scale=.6)
            self.assertTrue(1.0 <= g['target_rr'] <= 6.0)
        g = base_genes(target_rr=1.05)                                # a big downward step lands on the 1R bound
        low = G.mutate(dict(g), type('R', (), {'random': lambda s: 0.0, 'gauss': lambda s, a, b: -5.0, 'choice': lambda s, xs: xs[0]})())
        self.assertEqual(low['target_rr'], 1.0)
        self.assertNotIn('无止盈', G.describe(base_genes(target_rr=0.0)))
        self.assertIn('止盈 3.0R', G.describe(base_genes(target_rr=0.0)))

    def test_every_position_has_a_take_profit_even_with_a_zero_gene(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 5
        s = series_from(closes)
        g = base_genes(target_rr=0.0)
        p = X.simulate(g, {self.INST: s}, 30, s.n)['positions'][0]
        self.assertAlmostEqual(p['target'], p['entry'] + 3.0 * p['dist'])
        a = X.simulate_account([dict(id='m', genes=g, from_i=0)], {self.INST: s}, 30, s.n)['positions'][0]
        self.assertAlmostEqual(a['target'], a['entry'] + 3.0 * a['dist'])

    # ---- 2) reverse signal: own (default) / team, account-level
    def test_reverse_close_default_is_own_and_team_is_switchable(self):
        self.assertEqual(X.ACCOUNT_RULES['reverse_close'], 'own')
        s = self.flat()
        sets = {'A': ({0: [(self.INST, 1, 5.0)]}, {0: {self.INST: 1}, 6: {self.INST: -1}}),   # A opens a long, turns short on bar 6
                'B': ({}, {4: {self.INST: -1}})}                                                # B (seated) turns short on bar 4
        segs = [dict(id='a', genes=self.genes('A'), from_i=0), dict(id='b', genes=self.genes('B'), from_i=0)]
        own = self.account(sets, segs, s)
        self.assertEqual([(t['reason'], t['closed'], t['exit']) for t in own['trades']], [('reverse_signal', s.ts[6], 100.0 * (1 - X.SLIP))])
        team = self.account(sets, segs, s, rules=dict(X.ACCOUNT_RULES, reverse_close='team'))
        self.assertEqual([(t['reason'], t['closed']) for t in team['trades']], [('reverse_signal', s.ts[4])])
        # a member who has left the team no longer closes other members' positions under 'team'
        gone = self.account(sets, [segs[0], dict(segs[1], wind_i=3)], s, rules=dict(X.ACCOUNT_RULES, reverse_close='team'))
        self.assertEqual([t['closed'] for t in gone['trades']], [s.ts[6]])
        # a same-direction signal is not a reverse
        same = self.account({'A': ({0: [(self.INST, 1, 5.0)]}, {5: {self.INST: 1}}), 'B': ({}, {})}, segs, s)
        self.assertEqual((same['trades'], len(same['positions'])), ([], 1))
        with self.assertRaises(ValueError):
            self.account(sets, segs, s, rules=dict(X.ACCOUNT_RULES, reverse_close='any'))
        # the old era (bars on or before exit_from) has no reverse close
        old = self.account(sets, segs, s, exit_from=s.n)
        self.assertEqual((old['trades'], len(old['positions'])), ([], 1))

    def test_reverse_signals_ignore_the_direction_gene_and_the_volatility_filter(self):
        closes = [100.0] * 40 + [90.0] + [89.0] * 5                   # momentum breaks down on bar 40
        s = series_from(closes)
        for g in (base_genes(direction='long'), base_genes(vol_min=1.2, vol_max=1.21)):
            ev, rv = X.signal_sets(g, {self.INST: s})
            self.assertEqual(ev, {})                                   # no short entry for a long-only / filtered genome ...
            self.assertEqual(rv[40], {self.INST: -1})                  # ... but the short signal still closes a long
        ev, rv = X.signal_sets(base_genes(), {self.INST: s})
        self.assertEqual(ev, X.entries(base_genes(), {self.INST: s}))
        self.assertEqual(rv, X.reversals(base_genes(), {self.INST: s}))
        # simulate() (the backtest) closes on the genome's own reverse signal at that bar's close
        f = self.flat()
        r = X.simulate(self.genes('A'), {self.INST: f}, 0, f.n, events={0: [(self.INST, 1, 5.0)]}, reverse={5: {self.INST: -1}})
        self.assertEqual([(t['reason'], t['closed']) for t in r['trades']], [('reverse_signal', f.ts[5])])

    # ---- 3) trailing stop for every position
    def test_trailing_activates_at_one_r_and_only_moves_in_favour(self):
        # the PR #110 rule (1R / 1R), still used for bars before account['trail_risk_from']
        p = dict(side=1, entry=100.0, dist=2.0, stop=98.0)
        X.trail_update(p, 101.9, 99.0, X.PRIOR_TRAIL)
        self.assertEqual((p['stop'], p.get('trailing')), (98.0, None))       # < 1R: not yet
        X.trail_update(p, 102.0, 99.0, X.PRIOR_TRAIL)
        self.assertEqual((p['stop'], p['trailing']), (100.0, True))          # exactly 1R: breakeven
        X.trail_update(p, 105.0, 101.0, X.PRIOR_TRAIL)
        self.assertEqual(p['stop'], 103.0)                                    # best − 1R
        X.trail_update(p, 104.0, 99.0, X.PRIOR_TRAIL)
        self.assertEqual((p['stop'], p['best']), (103.0, 105.0))              # never loosens
        q = dict(side=-1, entry=100.0, dist=2.0, stop=102.0)
        X.trail_update(q, 101.0, 97.0, X.PRIOR_TRAIL)
        self.assertEqual(q['stop'], 99.0)
        X.trail_update(q, 103.0, 98.0, X.PRIOR_TRAIL)
        self.assertEqual(q['stop'], 99.0)
        self.assertEqual((X.PRIOR_TRAIL['start_r'], X.PRIOR_TRAIL['dist_r'], X.PRIOR_TRAIL['be_r']), (1.0, 1.0, None))

    def test_a_trailed_stop_hit_is_recorded_as_trailing_stop(self):
        closes = [100.0] * 40 + [110.0] + [112.0, 114.5, 116.0, 109.0, 109.0]
        s = series_from(closes)
        for r in (X.simulate(base_genes(), {self.INST: s}, 30, s.n),
                  X.simulate_account([dict(id='m', genes=base_genes(), from_i=0)], {self.INST: s}, 30, s.n)):
            self.assertEqual(r['trades'][0]['reason'], 'trailing_stop')
            self.assertGreater(r['trades'][0]['exit'], r['trades'][0]['entry'])     # the profit was protected

    # ---- 4) the switch point: closed history is never rewritten
    def test_trades_closed_before_exit_rules_from_are_unchanged(self):
        sr = {k: series_from(walk(900, n), k) for n, k in ((21, 'BTC-USDT-SWAP'), (22, 'ETH-USDT-SWAP'), (23, 'SOL-USDT-SWAP'))}
        seg = [dict(id='m', genes=base_genes(max_hold=12, trail=1, target_rr=0.0, lookback=15), from_i=0,
                    old_exit=dict(target_rr=0.0, trail=1, max_hold=12, tf=1))]
        n = 900
        old = X.simulate_account(seg, sr, 50, n, exit_from=n)        # every bar under the old exits
        self.assertIn('time', [t['reason'] for t in old['trades']])
        cut = 600
        mixed = X.simulate_account(seg, sr, 50, n, exit_from=cut)
        ts_cut = sr['BTC-USDT-SWAP'].ts[cut]
        before = lambda r: [t for t in r['trades'] if t['closed'] <= ts_cut]
        self.assertGreater(len(before(old)), 3)
        self.assertEqual(before(mixed), before(old))
        after = [t for t in mixed['trades'] if t['closed'] > ts_cut]
        self.assertNotIn('time', [t['reason'] for t in after])
        self.assertTrue(all(p['target'] for p in mixed['positions']))
        self.assertNotEqual(mixed['trades'], old['trades'])

    def test_positions_open_at_the_switch_get_a_target_and_the_trail(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 10
        s = series_from(closes)
        seg = [dict(id='m', genes=base_genes(target_rr=3.0), from_i=0, old_exit=dict(target_rr=0.0, trail=0, max_hold=240, tf=1))]
        old = X.simulate_account(seg, {self.INST: s}, 30, s.n, exit_from=s.n)['positions'][0]
        self.assertIsNone(old['target'])                               # stored gene had no target: none in the old era
        new = X.simulate_account(seg, {self.INST: s}, 30, s.n, exit_from=45)['positions'][0]
        self.assertAlmostEqual(new['target'], new['entry'] + 3.0 * new['dist'])
        self.assertEqual(new['opened'], old['opened'])

    def test_paper_update_stamps_exit_rules_from_and_publishes_targets_and_exits(self):
        acc = dict(from_ts=T0, start_equity=10000.0, segs=[], next=0)
        closes = [100.0] * 40 + [110.0] + [111.0] * 30
        sr = {self.INST: series_from(closes)}
        state = dict(nodes={}, team=[], paper=dict(account=acc, start_equity=10000.0))
        view, _ = P.update(state, sr, [], prev_last=sr[self.INST].ts[50])
        self.assertEqual(acc['exit_rules_from'], sr[self.INST].ts[50])
        self.assertEqual(view['rules']['reverse_close'], 'own')
        self.assertEqual(view['exits'], {})
        P.update(state, sr, [], prev_last=sr[self.INST].ts[-1])
        self.assertEqual(acc['exit_rules_from'], sr[self.INST].ts[50])   # set once, never moved
        acc2 = dict(from_ts=T0, start_equity=10000.0, segs=[], next=0)
        state2 = dict(nodes={}, team=[], paper=dict(account=acc2, start_equity=10000.0))
        P.update(state2, sr, [])
        self.assertEqual(acc2['exit_rules_from'], sr[self.INST].ts[-1])  # unknown previous run: this run's last bar
        # run.py reads the previous run's last bar from its dashboard.json
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'dashboard.json'
            self.assertIsNone(R.previous_last_bar(path))
            path.write_text(json.dumps(dict(paper=dict(curve=[[1, 2], [T0, 3]]))))
            self.assertEqual(R.previous_last_bar(path), T0)
        # a pending signal published for the OKX follow always carries tp and dist
        sig = P._signal_from_pending(self.INST, 1, base_genes(target_rr=0.0), sr, sr[self.INST].ts[-1], 'm')
        self.assertAlmostEqual(sig['tp'], sig['entry'] + 3.0 * sig['dist'])


class ManualCloseTests(unittest.TestCase):
    """The user's manual closes (paper.MANUAL_CLOSES → simulate_account(manual=...), reason manual_user): only that exact
    position, at its real price, never rewriting bars already published; afterwards the coin is free like any other."""
    A, B = 'BTC-USDT-SWAP', 'ETH-USDT-SWAP'

    def sr(self, n=20):
        ts = [T0 + i * H for i in range(n)]
        return {k: X.Series(k, ts, [100.0] * n, [100.1] * n, [99.99] * n, [100.0] * n) for k in (self.A, self.B)}

    def genes(self):
        return dict(name='A', risk=.05, leverage=10, target_rr=0, max_hold=240, tf=1, trail=0)

    def run_acc(self, entries, sr, manual=None, rules=X.ACCOUNT_RULES, **kw):
        with mock.patch.object(X, 'signal_sets', side_effect=lambda g, series: (entries, {})):
            return X.simulate_account([dict(id='a', genes=self.genes(), from_i=0)], sr, 0, next(iter(sr.values())).n,
                                      rules=rules, manual=manual, **kw)

    def test_closes_only_that_exact_position_at_its_price_with_reason_manual_user(self):
        sr = self.sr(); ts = sr[self.A].ts
        entries = {0: [(self.A, -1, 5.0), (self.B, -1, 5.0)]}                 # two shorts, both fill at bar 1's open
        base = self.run_acc(entries, sr)
        self.assertEqual(sorted(p['inst'] for p in base['positions']), [self.A, self.B])
        at = ts[4] + 19 * 60_000 + 27_000                                     # 04:19:27 → booked at the open of bar 5
        r = self.run_acc(entries, sr, manual={(self.A, ts[1]): (at, 97.5, None)})
        self.assertEqual(len(r['trades']), 1)
        t = r['trades'][0]
        self.assertEqual((t['inst'], t['reason'], t['exit'], t['opened'], t['closed'], t['booked']), (self.A, 'manual_user', 97.5, ts[1], at, ts[5]))
        p = next(p for p in base['positions'] if p['inst'] == self.A)
        fee_in = p['qty'] * p['entry'] * X.FEE
        self.assertAlmostEqual(t['pnl'], p['qty'] * (p['entry'] - 97.5) - p['qty'] * 97.5 * X.FEE - fee_in - p['funding'])   # no slip, taker fee
        self.assertEqual([q for q in r['positions'] if q['inst'] == self.B], [q for q in base['positions'] if q['inst'] == self.B])
        self.assertEqual(r['curve'][:5], base['curve'][:5])                   # nothing changes before the booking bar
        # another entry time of the same coin, or a coin not held, is never closed
        for key in ((self.A, ts[2]), ('SOL-USDT-SWAP', ts[1])):
            self.assertEqual(self.run_acc(entries, sr, manual={key: (at, 97.5, None)}), base, key)

    def test_bars_already_published_are_never_rewritten(self):
        sr = self.sr(); ts = sr[self.A].ts
        entries = {0: [(self.A, -1, 5.0)], 6: [(self.B, -1, 5.0)]}
        base = self.run_acc(entries, sr)
        r = self.run_acc(entries, sr, manual={(self.A, ts[1]): (ts[2] + 1000, 98.0, 9)})   # happened at bar 2, published up to bar 9
        t = next(t for t in r['trades'] if t['reason'] == 'manual_user')
        self.assertEqual((t['closed'], t['booked'], t['exit']), (ts[2] + 1000, ts[10], 98.0))
        self.assertEqual(r['curve'][:10], base['curve'][:10])                 # bars 0..9 exactly as before
        self.assertEqual([q for q in r['positions'] if q['inst'] == self.B], [q for q in base['positions'] if q['inst'] == self.B])
        early = self.run_acc(entries, sr, manual={(self.A, ts[1]): (ts[2] + 1000, 98.0, None)})
        self.assertEqual(next(t for t in early['trades'] if t['reason'] == 'manual_user')['booked'], ts[3])
        # a close time beyond the data does nothing yet (it is booked on the run that has that bar)
        self.assertEqual(self.run_acc(entries, sr, manual={(self.A, ts[1]): (ts[-1] + H, 98.0, None)}), base)
        # already gone by its own exit before the booking bar: nothing happens
        sr[self.A].h[4] = 106.0                                               # the stop (entry + 5) is hit on bar 4
        gone = self.run_acc({0: [(self.A, -1, 5.0)]}, sr)
        self.assertEqual([t['reason'] for t in gone['trades']], ['stop'])
        self.assertEqual(self.run_acc({0: [(self.A, -1, 5.0)]}, sr, manual={(self.A, ts[1]): (ts[8], 98.0, None)}), gone)

    @STOP_RISK
    def test_after_the_close_the_coin_reopens_and_it_no_longer_counts_for_same_side_or_crowding(self):
        sr = self.sr(); ts = sr[self.A].ts
        close_a = {(self.A, ts[1]): (ts[5], 98.0, None)}
        # same coin: a new short signal after the close opens a new position (new entry time), the manual close never touches it
        entries = {0: [(self.A, -1, 5.0)], 7: [(self.A, -1, 5.0)]}
        self.assertEqual([p['opened'] for p in self.run_acc(entries, sr)['positions']], [ts[1]])
        r = self.run_acc(entries, sr, manual=close_a)
        self.assertEqual([(p['inst'], p['opened']) for p in r['positions']], [(self.A, ts[8])])
        # same-side count: with max_same_side = 1 the ETH short is skipped while BTC is open, and fills once BTC was closed by hand
        entries = {0: [(self.A, -1, 5.0)], 7: [(self.B, -1, 5.0)]}
        one = dict(X.ACCOUNT_RULES, max_same_side=1)
        self.assertEqual([p['inst'] for p in self.run_acc(entries, sr, rules=one)['positions']], [self.A])
        self.assertEqual([p['inst'] for p in self.run_acc(entries, sr, manual=close_a, rules=one)['positions']], [self.B])
        # crowding (half from the 1st open same-side position here): full risk again after the manual close
        crowd = dict(X.ACCOUNT_RULES, crowd_steps=((1, .5),), major_factor=1.0)
        half = next(p for p in self.run_acc(entries, sr, rules=crowd)['positions'] if p['inst'] == self.B)
        full = next(p for p in self.run_acc(entries, sr, manual=close_a, rules=crowd)['positions'] if p['inst'] == self.B)
        self.assertAlmostEqual(half['risk'], .025); self.assertAlmostEqual(full['risk'], .05)

    def test_paper_update_stamps_manual_from_once_and_publishes_the_exit(self):
        sr = {self.A: series_from([100.0] * 30)}; ts = sr[self.A].ts
        acc = dict(from_ts=T0, start_equity=10000.0, next=1, segs=[dict(id='a1', champion='n1', slot=0, genes=base_genes(risk=.05), species='momentum',
                                                                       from_ts=T0, wind_ts=None, boosts=[], reason='carry')])
        state = dict(nodes={}, team=[], paper=dict(account=acc, start_equity=10000.0))
        key = f'{self.A}|{ts[1]}'
        with mock.patch.object(P, 'MANUAL_CLOSES', {(self.A, ts[1]): (ts[3] + 1000, 99.0)}), \
                mock.patch.object(X, 'signal_sets', side_effect=lambda g, series: ({0: [(self.A, -1, 5.0)]}, {})):
            view, signals = P.update(state, sr, [], prev_last=ts[6])
            self.assertEqual(acc['manual_from'], {key: ts[6]})
            t = next(t for t in view['trades'] if t['reason'] == 'manual_user')
            self.assertEqual((t['exit'], t['closed'], t['booked']), (99.0, ts[3] + 1000, ts[7]))
            self.assertEqual(view['exits'], {key: 'manual_user'})
            self.assertNotIn(self.A, signals)
            P.update(state, sr, [], prev_last=ts[-1])
            self.assertEqual(acc['manual_from'], {key: ts[6]})                # set once, never moved
        # the shipped config: only these seven shorts, nothing else (ETH-USDT-SWAP|1795219200000 is not in it)
        self.assertEqual(sorted(f'{i}|{o}' for i, o in P.MANUAL_CLOSES), sorted([
            'NEAR-USDT-SWAP|1794700800000', 'ADA-USDT-SWAP|1794700800000', 'LTC-USDT-SWAP|1794700800000', 'KAITO-USDT-SWAP|1794816000000',
            'ICP-USDT-SWAP|1794931200000', 'H-USDT-SWAP|1794384000000', 'DOGE-USDT-SWAP|1794499200000']))
        self.assertEqual(P.MANUAL_CLOSES[('NEAR-USDT-SWAP', 1794700800000)], (1795213167000, 2.87))
        self.assertEqual(P.MANUAL_CLOSES[('H-USDT-SWAP', 1794384000000)], (1795262400000, 0.01098))
        self.assertEqual(P.MANUAL_CLOSES[('DOGE-USDT-SWAP', 1794499200000)], (1795262400000, 0.05372))


class TrailRiskRuleTests(unittest.TestCase):
    """execution TRAIL & RISK RULES (2026-11-21): trailing start / breakeven, same-champion same-bar same-direction risk
    share, new / winding seat half risk, and the paper cutover account['trail_risk_from']."""
    A, B, C = 'AAA-USDT-SWAP', 'BBB-USDT-SWAP', 'CCC-USDT-SWAP'
    OLD = -10 ** 15            # a seat held since long ago (the new-seat rule does not apply)

    def sr(self, n=60):
        ts = [T0 + i * H for i in range(n)]
        return {k: X.Series(k, ts, [100.0] * n, [100.1] * n, [99.99] * n, [100.0] * n) for k in (self.A, self.B, self.C)}

    def genes(self, name):
        return dict(name=name, risk=.05, leverage=10, target_rr=3.0, max_hold=240, tf=1, trail=0)

    def seg(self, name, **kw):
        return dict(dict(id=name, genes=self.genes(name), from_i=0, tenure_ts=self.OLD), **kw)

    def run_acc(self, sets, segs, sr=None, **kw):
        sr = sr or self.sr()
        with mock.patch.object(X, 'signal_sets', side_effect=lambda g, series: (sets.get(g['name'], {}), {})):
            return X.simulate_account(segs, sr, 0, next(iter(sr.values())).n, **kw)

    def risks(self, r):
        return {p['inst']: (round(p['risk'], 6), round(p['risk_factor'], 6)) for p in r['positions']}

    # ---- a) trailing / breakeven
    def test_defaults(self):
        self.assertEqual((X.TRAIL_START_R, X.TRAIL_DIST_R, X.BREAKEVEN_R), (.5, 1.0, None))
        self.assertEqual(X.TRAIL, dict(start_r=.5, dist_r=1.0, be_r=None))
        self.assertEqual((X.SAME_BAR_SHARE, X.NEW_SEAT_HOURS, X.NEW_SEAT_FACTOR), (True, 24, .5))
        self.assertEqual(X.PRIOR_TRAIL, dict(start_r=1.0, dist_r=1.0, be_r=None))
        for k in ('trail', 'same_bar_share', 'new_seat_h', 'new_seat_factor'):
            self.assertIn(k, X.ACCOUNT_RULES)
        self.assertEqual((X.MIN_TRADE_RISK, X.MAX_TRADE_RISK, X.ACCOUNT_RULES['daily_halt']), (.05, .10, .25))   # unchanged

    def test_half_r_start_then_trails_one_r_behind(self):
        lvl = lambda side, best, stop, t=None: X.trail_stop_level(side, 100.0, best, stop, 2.0, t)
        self.assertEqual(lvl(1, 100.9, 98.0), (98.0, False))           # 0.45 R
        self.assertEqual(lvl(1, 101.0, 98.0), (99.0, True))            # 0.5 R: stop = best − 1R (loss halved)
        self.assertEqual(lvl(1, 102.0, 99.0), (100.0, True))           # 1 R: breakeven
        self.assertEqual(lvl(1, 101.0, 100.0), (100.0, True))          # never loosens
        self.assertEqual(lvl(-1, 99.0, 102.0), (101.0, True))
        self.assertEqual(lvl(1, 101.9, 98.0, X.PRIOR_TRAIL), (98.0, False))   # the old 1 R start
        self.assertEqual(X.trail_stop_level(1, 100.0, 105.0, 98.0, 0.0), (98.0, False))   # no unit: nothing

    def test_breakeven_step(self):
        be = dict(start_r=1.0, dist_r=1.0, be_r=.5)
        self.assertEqual(X.trail_stop_level(1, 100.0, 100.9, 98.0, 2.0, be), (98.0, False))
        self.assertEqual(X.trail_stop_level(1, 100.0, 101.0, 98.0, 2.0, be), (100.0, True))    # 0.5 R → entry
        self.assertEqual(X.trail_stop_level(-1, 100.0, 99.0, 102.0, 2.0, be), (100.0, True))
        self.assertEqual(X.trail_stop_level(1, 100.0, 103.0, 98.0, 2.0, be), (101.0, True))    # then the trail
        p = dict(side=1, entry=100.0, dist=2.0, stop=98.0)
        X.trail_update(p, 101.2, 99.5, be)
        self.assertEqual((p['stop'], p['trailing'], p['best']), (100.0, True, 101.2))

    def test_trail_unit_min_and_fallback(self):
        self.assertEqual(X.trail_unit(88.316, 86.25), 86.25)
        self.assertEqual(X.trail_unit(88.316, None), 88.316)
        self.assertEqual(X.trail_unit(None, 86.25), 86.25)
        self.assertEqual(X.trail_unit(0, 0), 0.0)
        # ETH 2026-11-21: best 1536.28 from the live entry 1623.71 = 1.014 R by the live dist, 0.99 R by the paper dist
        one = X.PRIOR_TRAIL
        self.assertFalse(X.trail_stop_level(-1, 1623.71, 1536.28, 1709.96, 88.316, one)[1])
        stop, on = X.trail_stop_level(-1, 1623.71, 1536.28, 1709.96, X.trail_unit(88.316, 1709.96 - 1623.71), one)
        self.assertTrue(on)
        self.assertAlmostEqual(stop, 1536.28 + 86.25, places=6)        # 1622.53: just below the live entry (short)

    # ---- b, c) the risk factor
    def test_entry_risk_factor(self):
        f = X.entry_risk_factor
        self.assertEqual(f(1), 1.0)
        self.assertEqual(f(2), .5)
        self.assertAlmostEqual(f(3), 1 / 3)
        self.assertEqual(f(1, seat_age_h=10, first=True), .5)
        self.assertEqual(f(1, seat_age_h=10, first=False), 1.0)        # only the first entry bar
        self.assertEqual(f(1, seat_age_h=24, first=True), 1.0)         # held 24 h: full
        self.assertEqual(f(1, seat_age_h=500, winding=True), .5)       # winding: half
        self.assertEqual(f(2, seat_age_h=1, first=True), .25)          # AVAX + FIL from a new seat: a quarter each
        self.assertEqual(f(2, rules=dict(same_bar_share=False, new_seat_h=None)), 1.0)
        self.assertEqual(f(1, seat_age_h=30, first=True, rules=dict(new_seat_h=48)), .5)

    def test_same_bar_groups(self):
        pend = [('a', self.A, 1, 5.0, False), ('a', self.B, 1, 5.0, False), ('a', self.C, -1, 5.0, False),
                ('b', self.A, 1, 5.0, False), ('b', self.C, 1, 5.0, False)]
        # b's A and C were signalled first by a (fill order): one position per coin, so b has no group
        self.assertEqual(X.same_bar_groups(pend, set(), {}), {('a', 1): 2, ('a', -1): 1})
        self.assertEqual(X.same_bar_groups(pend, {self.A}, {}), {('a', 1): 1, ('a', -1): 1})
        self.assertEqual(X.same_bar_groups(pend[3:], set(), {}), {('b', 1): 2})
        self.assertEqual(X.same_bar_groups(pend[:2], set(), {'a': 2}), {('a', 1): 1})          # one free slot of 3
        self.assertEqual(X.same_bar_groups(pend[:2], set(), {}, eligible=lambda k: k != self.B), {('a', 1): 1})
        self.assertEqual(X.same_bar_groups([('a', self.A, 1, 0.0)], set(), {}), {})

    @STOP_RISK
    def test_same_champion_same_bar_same_direction_share_one_risk(self):
        r = self.run_acc({'a': {0: [(self.A, 1, 5.0), (self.B, 1, 5.0)]}}, [self.seg('a')])
        self.assertEqual(self.risks(r), {self.A: (.025, .5), self.B: (.025, .5)})
        total = sum(p['risk'] for p in r['positions'])
        self.assertAlmostEqual(total, .05)                              # the group ≤ one position's normal risk

    @STOP_RISK
    def test_different_direction_champion_or_bar_are_not_grouped(self):
        full = (.05, 1.0)
        r = self.run_acc({'a': {0: [(self.A, 1, 5.0), (self.B, -1, 5.0)]}}, [self.seg('a')])
        self.assertEqual(self.risks(r), {self.A: full, self.B: full})
        r = self.run_acc({'a': {0: [(self.A, 1, 5.0)]}, 'b': {0: [(self.B, 1, 5.0)]}}, [self.seg('a'), self.seg('b')])
        self.assertEqual(self.risks(r), {self.A: full, self.B: full})
        r = self.run_acc({'a': {0: [(self.A, 1, 5.0)], 3: [(self.B, 1, 5.0)]}}, [self.seg('a')])
        self.assertEqual(self.risks(r), {self.A: full, self.B: full})
        # a coin taken first by another seat in the same pass does not count for the later seat's group
        r = self.run_acc({'a': {0: [(self.A, 1, 5.0)]}, 'b': {0: [(self.A, 1, 5.0), (self.B, 1, 5.0)]}}, [self.seg('a'), self.seg('b')])
        self.assertEqual(self.risks(r), {self.A: full, self.B: full})

    @STOP_RISK
    def test_new_seat_first_entry_half_then_full(self):
        sr = self.sr(); ts = sr[self.A].ts
        sets = {'a': {0: [(self.A, 1, 5.0)], 5: [(self.B, 1, 5.0)]}}
        r = self.run_acc(sets, [self.seg('a', tenure_ts=ts[0])], sr)
        self.assertEqual(self.risks(r), {self.A: (.025, .5), self.B: (.05, 1.0)})   # 1 h old: first half, second full
        r = self.run_acc(sets, [self.seg('a', tenure_ts=ts[1] - 24 * H)], sr)
        self.assertEqual(self.risks(r), {self.A: (.05, 1.0), self.B: (.05, 1.0)})   # 24 h at the fill: full
        r = self.run_acc(sets, [self.seg('a', tenure_ts=ts[0])], sr, rules=dict(X.ACCOUNT_RULES, new_seat_h=None))
        self.assertEqual(self.risks(r), {self.A: (.05, 1.0), self.B: (.05, 1.0)})   # rule off
        # first entry bar of a new seat with two entries: shared AND halved
        r = self.run_acc({'a': {0: [(self.A, 1, 5.0), (self.B, 1, 5.0)]}}, [self.seg('a', tenure_ts=ts[0])], sr)
        self.assertEqual(self.risks(r), {self.A: (.0125, .25), self.B: (.0125, .25)})

    @STOP_RISK
    def test_a_seat_chain_keeps_its_first_fill(self):
        sr = self.sr(); ts = sr[self.A].ts
        segs = [self.seg('a', tenure_ts=ts[0], chain='s', wind_i=4), self.seg('b', tenure_ts=ts[0], chain='s', from_i=4)]
        r = self.run_acc({'a': {0: [(self.A, 1, 5.0)]}, 'b': {5: [(self.B, 1, 5.0)]}}, segs, sr)
        self.assertEqual(self.risks(r)[self.B], (.05, 1.0))            # the seat already traded: not its first entry

    @STOP_RISK
    def test_winding_seat_entry_is_half(self):
        r = self.run_acc({'a': {0: [(self.A, 1, 5.0)]}}, [self.seg('a', wind_i=1)])
        self.assertEqual(self.risks(r), {self.A: (.025, .5)})           # filled on the bar the seat was wound
        r = self.run_acc({'a': {0: [(self.A, 1, 5.0)]}}, [self.seg('a', wind_i=2)])
        self.assertEqual(self.risks(r), {self.A: (.05, 1.0)})

    def test_simulate_shares_the_same_bar_risk_of_one_genome(self):
        sr = self.sr()
        g = self.genes('a')
        ev = {0: [(self.A, 1, 5.0), (self.B, 1, 5.0)]}
        r = X.simulate(g, sr, 0, 60, events=ev, reverse={})
        q = {p['inst']: p['qty'] for p in r['positions']}
        off = X.simulate(g, sr, 0, 60, events=ev, reverse={}, rules=dict(X.ACCOUNT_RULES, same_bar_share=False))
        self.assertAlmostEqual(q[self.A] * 2, next(p['qty'] for p in off['positions'] if p['inst'] == self.A), delta=1e-6)

    # ---- the paper cutover
    def test_trail_risk_from_keeps_history_identical(self):
        sr = {k: series_from(walk(900, n), k) for n, k in ((21, 'BTC-USDT-SWAP'), (22, 'ETH-USDT-SWAP'), (23, 'SOL-USDT-SWAP'))}
        ts = sr['BTC-USDT-SWAP'].ts
        segs = [dict(id='m', genes=base_genes(lookback=15), from_i=0, tenure_ts=ts[0]),
                dict(id='k', genes=base_genes(lookback=25, species='momentum'), from_i=200, tenure_ts=ts[200])]
        prior = dict(X.ACCOUNT_RULES, **PRIOR_ERA)
        old = X.simulate_account(segs, sr, 50, 900, rules=prior)
        self.assertEqual(X.simulate_account(segs, sr, 50, 900, trail_risk_from=899), old)   # cutover at the end = before
        cut = 600
        mixed = X.simulate_account(segs, sr, 50, 900, trail_risk_from=cut)
        before = lambda r: [t for t in r['trades'] if t['closed'] <= ts[cut]]
        self.assertGreater(len(before(old)), 3)
        self.assertEqual(before(mixed), before(old))
        self.assertEqual(mixed['curve'][:cut - 50 + 1], old['curve'][:cut - 50 + 1])
        self.assertTrue(all('risk_factor' not in t for t in before(mixed)))     # old trades keep their published fields
        self.assertNotEqual(mixed['trades'], old['trades'])

    def test_paper_update_stamps_trail_risk_from_once_and_publishes_the_rules(self):
        acc = dict(from_ts=T0, start_equity=10000.0, segs=[], next=0)
        sr = {'BTC-USDT-SWAP': series_from([100.0] * 70)}
        state = dict(nodes={}, team=[], paper=dict(account=acc, start_equity=10000.0))
        view, _ = P.update(state, sr, [], prev_last=sr['BTC-USDT-SWAP'].ts[50])
        self.assertEqual(acc['trail_risk_from'], sr['BTC-USDT-SWAP'].ts[50])
        P.update(state, sr, [], prev_last=sr['BTC-USDT-SWAP'].ts[-1])
        self.assertEqual(acc['trail_risk_from'], sr['BTC-USDT-SWAP'].ts[50])   # never moved
        self.assertEqual((view['rules']['trail'], view['rules']['new_seat_h'], view['rules']['same_bar_share']), (X.TRAIL, 24, True))

    def test_seat_chains(self):
        segs = [dict(id='a1', champion='x', from_ts=0, wind_ts=10), dict(id='a2', champion='x', from_ts=10, wind_ts=None),
                dict(id='a3', champion='y', from_ts=5, wind_ts=8), dict(id='a4', champion='y', from_ts=20, wind_ts=None)]
        chain, tenure = P.seat_chains(segs)
        self.assertEqual(chain, dict(a1='a1', a2='a1', a3='a3', a4='a4'))     # a re-seat after a gap is a new seat
        self.assertEqual(tenure, dict(a1=0, a2=0, a3=5, a4=20))


class NextHourRuleTests(unittest.TestCase):
    """NEXT-HOUR RULE (paper.py): a GitHub run stamps every change on C = floor(now, 1 h) + 1 h, so an hourly server that
    replays the published state.json gets the same signals from a stale or a fresh state until bar C exists, and the same
    as the next GitHub run afterwards."""
    COINS = ('A-USDT-SWAP', 'B-USDT-SWAP', 'C-USDT-SWAP', 'D-USDT-SWAP', 'E-USDT-SWAP', 'F-USDT-SWAP')
    L = 20                     # the GitHub run's last bar index; it runs ~57 min late: now = ts[L] + 2 h − 3 min

    def setUp(self):
        gate = mock.patch.object(P, 'entry_gate', lambda node, state=None, streak=None: dict(passed=True, reasons=[]))
        gate.start(); self.addCleanup(gate.stop)
        self.entries = {}       # {lookback gene: {bar: [(inst, side, dist)]}}: each strategy's signals, by bar
        sig = mock.patch.object(X, 'signal_sets', side_effect=lambda g, series: (self.entries.get(int(g['lookback']), {}), {}))
        sig.start(); self.addCleanup(sig.stop)
        mc = mock.patch.object(P, 'MANUAL_CLOSES', {})     # the shipped closes are for real coins; tests add their own
        mc.start(); self.addCleanup(mc.stop)

    def sr(self, n):
        ts = [T0 + i * H for i in range(n)]
        return {k: X.Series(k, ts, [100.0] * n, [100.1] * n, [99.99] * n, [100.0] * n) for k in self.COINS}

    def ts(self, i):
        return T0 + i * H

    def node(self, nid, lookback):
        return dict(id=nid, genes=base_genes(lookback=lookback, risk=.05), species='momentum', fitness=1.0, qualified=True)

    def state(self, team=('n1',), leader=None):
        nodes = {nid: self.node(nid, lb) for nid, lb in (('n1', 11), ('n2', 12), ('n3', 13))}
        segs = [dict(id=f'a{k + 1}', champion=nid, slot=k, genes=fix_genes(nodes[nid]['genes']), species='momentum', from_ts=T0, wind_ts=None,
                     boosts=[[T0, None]] if nid == leader else [], reason='carry') for k, nid in enumerate(team)]
        acc = dict(from_ts=T0, start_equity=10000.0, next=len(segs), segs=segs, side_cap_from=T0, risk_rules_from=T0, risk_cap_from=T0,
                   exit_rules_from=T0, trail_risk_from=T0, manual_from={})
        return dict(nodes=nodes, team=[dict(id=nid, since=T0) for nid in team], leader=leader, champion=leader,
                    paper=dict(account=acc, start_equity=10000.0))

    def replay(self, state, n, price_off=None):
        """The hourly server: replay only, on a copy (it never writes state.json)."""
        st = json.loads(json.dumps(state))
        cap, orig = {}, X.simulate_account
        with mock.patch.object(P, 'simulate_account', side_effect=lambda *a, **k: cap.setdefault('r', orig(*a, **k))):
            view, signals = P.update(st, self.sr(n), [], price_off, replay=True)
        self.assertEqual(st, json.loads(json.dumps(state)))                          # replay never changes the state
        return dict(signals=signals, trades=cap['r']['trades'], curve=cap['r']['curve'], positions=view['positions'], pending=view['pending'])

    def github(self, state, n, changes, now, price_off=None):
        return P.update(state, self.sr(n), changes, price_off, now=now)

    def late_now(self):
        return self.ts(self.L) + 2 * H - 3 * 60_000                                   # 57 min late → C = ts[L] + 2 h

    # --- the definition -------------------------------------------------------------------------------------------
    def test_effective_bar_is_the_next_whole_hour_and_its_boundaries(self):
        h17 = T0 + 17 * H
        self.assertEqual(P.effective_bar(h17), h17 + H)                              # exactly on the hour → the next hour
        self.assertEqual(P.effective_bar(h17 + 5_000), h17 + H)                      # a few seconds after
        self.assertEqual(P.effective_bar(h17 - 1), h17)                              # 16:59:59.999 → 17:00
        self.assertEqual(P.effective_bar(h17 - 60_000), h17)                         # :59
        self.assertEqual(P.effective_bar(h17 + H - 1), h17 + H)
        for now, want in ((self.ts(self.L) + 2 * H, self.ts(self.L) + 3 * H), (self.ts(self.L) + 2 * H + 4_000, self.ts(self.L) + 3 * H),
                          (self.ts(self.L) + 2 * H - 1_000, self.ts(self.L) + 2 * H)):
            st = self.state(team=('n1',))
            st['team'] = [dict(id='n2', since=now)]
            self.github(st, self.L + 1, [(0, 'n1', 'n2', 'challenger')], now)
            segs = st['paper']['account']['segs']
            self.assertEqual((segs[0]['wind_ts'], segs[1]['from_ts'], st['paper']['account']['next_hour_from']), (want, want, want))
        # never on or before the run's own last bar, whatever the clock says
        st = self.state(team=('n1',)); st['team'] = [dict(id='n2', since=0)]
        self.github(st, self.L + 1, [(0, 'n1', 'n2', 'challenger')], self.ts(self.L) - 5 * H)
        self.assertEqual(st['paper']['account']['segs'][1]['from_ts'], self.ts(self.L) + H)

    # --- seats --------------------------------------------------------------------------------------------------------
    def test_seat_change_takes_effect_on_bar_C_and_the_server_never_diverges(self):
        L, C = self.L, self.ts(self.L) + 2 * H                                       # bar index L + 2 has open time C
        self.entries = {11: {L + 1: [('A-USDT-SWAP', 1, 5.0)], L + 2: [('C-USDT-SWAP', 1, 5.0)]},     # n1 (outgoing)
                        12: {L + 1: [('B-USDT-SWAP', 1, 5.0)], L + 2: [('D-USDT-SWAP', 1, 5.0)]}}     # n2 (incoming)
        stale = self.state(team=('n1',))
        self.github(stale, L - 3, [], self.late_now() - 4 * H)                       # the previous run (4 h earlier): rule switched on
        fresh = json.loads(json.dumps(stale))
        fresh['team'] = [dict(id='n2', since=self.late_now())]
        view, signals = self.github(fresh, L + 1, [(0, 'n1', 'n2', 'challenger')], self.late_now())
        a1, a2 = fresh['paper']['account']['segs']
        self.assertEqual((a1['wind_ts'], a2['from_ts']), (C, C))                     # stamped on C, not on ts[L]
        self.assertEqual([x['kind'] for x in view['scheduled']], ['seat', 'wind'])  # pending: C is not in the data yet
        self.assertEqual(next(m for m in view['members'] if m['id'] == 'n2')['effective_from'], C)
        self.assertFalse(next(m for m in view['members'] if m['id'] == 'n2')['trading'])
        # server at C:01:30 (last bar C − 1 h = L + 1): stale and fresh state.json give the same result (except `rank`,
        # which only orders the follow's entries and always follows the latest team / fitness — see the contract)
        self.assertEqual(unranked(self.replay(stale, L + 2)), unranked(self.replay(fresh, L + 2)))
        self.assertEqual([s['rank'] for s in self.replay(fresh, L + 2)['signals'].values()], [100])   # n1 left the team list
        self.assertEqual([(p['inst'], p['champion']) for p in self.replay(fresh, L + 2)['pending']], [('A-USDT-SWAP', 'n1')])
        # server at C + 1 h:01:30 (last bar C = L + 2): n1's signal of bar C − 1 h filled at C; on bar C only n2 signals
        r = self.replay(fresh, L + 3)
        self.assertEqual([(p['inst'], p['champion']) for p in r['positions']], [('A-USDT-SWAP', 'n1')])
        self.assertTrue(r['positions'][0]['winding'])
        self.assertEqual([(p['inst'], p['champion']) for p in r['pending']], [('D-USDT-SWAP', 'n2')])
        # … and it is exactly what the next GitHub run replays (deterministic, same input → same output)
        for n in (L + 3, L + 6):
            nxt = json.loads(json.dumps(fresh))
            v, sg = self.github(nxt, n, [], self.late_now() + 4 * H)
            self.assertEqual(sg, self.replay(fresh, n)['signals'])
            self.assertEqual(nxt['paper']['account']['segs'], fresh['paper']['account']['segs'])    # nothing re-stamped
        # the old rule (stamp = the run's last bar) would have moved n1's bar-(L+1) signal to n2: that is the divergence
        legacy = self.state(team=('n1',)); legacy['team'] = [dict(id='n2', since=0)]
        P.update(legacy, self.sr(L + 1), [(0, 'n1', 'n2', 'challenger')])
        self.assertEqual(legacy['paper']['account']['segs'][1]['from_ts'], self.ts(L))
        self.assertEqual([p['inst'] for p in P.update(legacy, self.sr(L + 3), [])[0]['positions']], ['B-USDT-SWAP'])

    def test_two_runs_in_the_same_hour_leave_an_empty_segment_that_never_trades(self):
        L = self.L
        self.entries = {12: {L + 2: [('B-USDT-SWAP', 1, 5.0)]}, 13: {L + 2: [('C-USDT-SWAP', 1, 5.0)]}}
        st = self.state(team=('n1',))
        st['team'] = [dict(id='n2', since=0)]; self.github(st, L + 1, [(0, 'n1', 'n2', 'challenger')], self.late_now())
        st['team'] = [dict(id='n3', since=0)]; self.github(st, L + 1, [(0, 'n2', 'n3', 'challenger')], self.late_now() + 60_000)
        a2 = st['paper']['account']['segs'][1]
        self.assertEqual(a2['from_ts'], a2['wind_ts'])
        self.assertEqual([(p['inst'], p['champion']) for p in self.replay(st, L + 3)['pending']], [('C-USDT-SWAP', 'n3')])

    # --- leader -------------------------------------------------------------------------------------------------------
    def test_leader_change_takes_effect_on_bar_C(self):
        L, C = self.L, self.ts(self.L) + 2 * H
        self.entries = {11: {L + 1: [('A-USDT-SWAP', 1, 5.0)], L + 2: [('B-USDT-SWAP', 1, 5.0)]}}
        stale = self.state(team=('n1', 'n2'))
        self.github(stale, L - 3, [], self.late_now() - 4 * H)
        fresh = json.loads(json.dumps(stale)); fresh['leader'] = 'n1'
        view, _ = self.github(fresh, L + 1, [], self.late_now())
        self.assertEqual(fresh['paper']['account']['segs'][0]['boosts'], [[C, None]])
        self.assertEqual([x['kind'] for x in view['scheduled']], ['leader'])
        self.assertEqual(self.replay(stale, L + 2)['signals'], self.replay(fresh, L + 2)['signals'])  # no `leader` flag yet
        r = self.replay(fresh, L + 4)
        boost = {p['inst']: p['boost'] for p in r['positions']}
        self.assertEqual(boost, {'A-USDT-SWAP': False, 'B-USDT-SWAP': True})       # bar C − 1 h signal: not leader; bar C: leader
        # losing the title closes the range on the next C
        later = self.ts(L) + 5 * H + 600_000
        fresh['leader'] = None
        self.github(fresh, L + 4, [], later)
        self.assertEqual(fresh['paper']['account']['segs'][0]['boosts'], [[C, P.effective_bar(later)]])

    # --- price_off ------------------------------------------------------------------------------------------------------
    def test_price_off_is_stamped_on_the_first_fill_after_C(self):
        L, C = self.L, self.ts(self.L) + 2 * H
        self.entries = {11: {L + 1: [('A-USDT-SWAP', 1, 5.0)], L + 2: [('B-USDT-SWAP', 1, 5.0)]}}
        st = self.state(team=('n1',)); self.github(st, L - 3, [], self.late_now() - 4 * H)
        with tempfile.TemporaryDirectory() as tmp:                                    # run.py: price_check(…, C + 1 h)
            (Path(tmp) / 'evolve').mkdir()
            (Path(tmp) / 'evolve' / 'universe.json').write_text(json.dumps(dict(instruments=['A-USDT-SWAP', 'B-USDT-SWAP'])))
            tick = lambda **px: [dict(instId=f'{k}-USDT-SWAP', last=str(v)) for k, v in px.items()]
            off, _ = D.price_check(tmp, P.effective_bar(self.late_now()) + H, lambda p: tick(A=100, B=100), lambda p: tick(A=150, B=150))
        self.assertEqual(off, {'A-USDT-SWAP': [[C + H, None]], 'B-USDT-SWAP': [[C + H, None]]})
        # server at C:01:30: A's fill at C is not blocked, with or without the new ranges
        self.assertEqual(self.replay(st, L + 2, off), self.replay(st, L + 2, {}))
        self.assertEqual([p['inst'] for p in self.replay(st, L + 2, off)['pending']], ['A-USDT-SWAP'])
        # server at C + 1 h:01:30: B's fill at C + 1 h is blocked, so it is not queued — and the replay agrees later
        self.assertEqual(self.replay(st, L + 3, off)['pending'], [])
        self.assertEqual([p['inst'] for p in self.replay(st, L + 5, off)['positions']], ['A-USDT-SWAP'])
        view, _ = self.github(json.loads(json.dumps(st)), L + 1, [], self.late_now(), off)
        self.assertEqual([x['kind'] for x in view['scheduled']], ['price_off', 'price_off'])
        self.assertEqual(view['price_off'], ['A-USDT-SWAP', 'B-USDT-SWAP'])         # the dashboard shows them as off already
        # a range closed on C + 1 h keeps blocking the fill at C (old code: the queue unblocked at once)
        closed = {'A-USDT-SWAP': [[self.ts(5), C + H]]}
        self.assertEqual(self.replay(st, L + 2, closed)['pending'], [])
        self.assertEqual([p['inst'] for p in self.replay(st, L + 3, closed)['pending']], ['B-USDT-SWAP'])

    # --- manual closes ----------------------------------------------------------------------------------------------------
    def test_manual_close_is_stamped_on_C_and_booked_at_the_open_of_C_plus_1h(self):
        L, C = self.L, self.ts(self.L) + 2 * H
        self.entries = {11: {3: [('A-USDT-SWAP', -1, 5.0)], L + 2: [('B-USDT-SWAP', -1, 5.0)]}}
        st = self.state(team=('n1',)); self.github(st, L - 3, [], self.late_now() - 4 * H)
        key, at = f'A-USDT-SWAP|{self.ts(4)}', self.ts(L) + 30 * 60_000                # closed by hand at ts[L] + 30 min
        stale = json.loads(json.dumps(st))
        with mock.patch.object(P, 'MANUAL_CLOSES', {('A-USDT-SWAP', self.ts(4)): (at, 99.0)}):
            # the server already runs the code with the new entry, but GitHub has not stamped it: pending, nothing changes
            self.assertEqual(self.replay(stale, L + 4), self.replay(st, L + 4))
            self.assertEqual([p['inst'] for p in self.replay(stale, L + 4)['positions']], ['A-USDT-SWAP', 'B-USDT-SWAP'])
            view, _ = self.github(st, L + 1, [], self.late_now())
            self.assertEqual(st['paper']['account']['manual_from'], {key: C})
            self.assertEqual(view['scheduled'], [dict(kind='manual_close', ts=C, key=key)])
            self.assertEqual(self.replay(st, L + 2), self.replay(stale, L + 2))     # server at C:01:30
            r = self.replay(st, L + 3)                                               # server at C + 1 h:01:30: still open
            self.assertEqual([p['inst'] for p in r['positions']], ['A-USDT-SWAP'])
            self.assertEqual([p['inst'] for p in r['pending']], ['B-USDT-SWAP'])
            r = self.replay(st, L + 4)                                               # booked at the open of C + 1 h
            t = next(t for t in r['trades'] if t['reason'] == 'manual_user')
            self.assertEqual((t['closed'], t['booked'], t['exit']), (at, C + H, 99.0))
            nxt = json.loads(json.dumps(st)); _, sg = self.github(nxt, L + 4, [], self.late_now() + 4 * H)
            self.assertEqual(sg, r['signals']); self.assertEqual(nxt['paper']['account']['manual_from'], {key: C})

    def test_queue_counts_a_manual_close_booked_at_the_next_open_as_gone(self):
        L, C = self.L, self.ts(self.L) + 2 * H
        self.entries = {11: {3: [('A-USDT-SWAP', -1, 5.0)], L + 2: [('B-USDT-SWAP', -1, 5.0)]}}
        st = self.state(team=('n1',))
        with mock.patch.object(P, 'MANUAL_CLOSES', {('A-USDT-SWAP', self.ts(4)): (self.ts(L), 99.0)}), \
                mock.patch.dict(X.ACCOUNT_RULES, max_same_side=1):
            self.github(st, L + 1, [], self.late_now())
            # last bar C: A (short) is still open, but it is booked at the next open before the fills → B fits under the cap
            r = self.replay(st, L + 3)
            self.assertEqual([p['inst'] for p in r['pending']], ['B-USDT-SWAP'])
            self.assertEqual([p['inst'] for p in self.replay(st, L + 4)['positions']], ['B-USDT-SWAP'])

    # --- cutover keys -------------------------------------------------------------------------------------------------------
    def test_cutover_keys_are_first_set_on_C_and_never_moved(self):
        L, C = self.L, self.ts(self.L) + 2 * H
        st = self.state(team=('n1',))
        acc = st['paper']['account']
        for k in P.CUTOVER_KEYS + ('manual_from',):
            acc.pop(k, None)
        with mock.patch.object(P, 'MANUAL_CLOSES', {('A-USDT-SWAP', self.ts(4)): (self.ts(2), 99.0)}):
            view, _ = self.github(st, L + 1, [], self.late_now(), )
            self.assertEqual({k: acc[k] for k in P.CUTOVER_KEYS + ('next_hour_from',)}, {k: C for k in P.CUTOVER_KEYS + ('next_hour_from',)})
            self.assertEqual(acc['manual_from'], {f'A-USDT-SWAP|{self.ts(4)}': C})
            self.assertEqual(view['rules']['next_hour_from'], C)
            self.assertEqual(view['rules']['exit_rules_from'], C); self.assertEqual(view['rules']['trail_risk_from'], C)
            self.github(st, L + 8, [], self.late_now() + 7 * H)
            self.assertEqual({k: acc[k] for k in P.CUTOVER_KEYS + ('next_hour_from',)}, {k: C for k in P.CUTOVER_KEYS + ('next_hour_from',)})

    def test_exit_and_trail_eras_switch_after_bar_C(self):
        L, C = self.L, self.ts(self.L) + 2 * H
        self.entries = {11: {L + 1: [('A-USDT-SWAP', 1, 5.0)]}}
        st = self.state(team=('n1',))
        acc = st['paper']['account']
        acc.pop('exit_rules_from'); acc.pop('trail_risk_from')
        acc['segs'][0]['genes']['target_rr'] = 0                                    # stored as before 2026-11-21: no target
        stale = json.loads(json.dumps(st)); stale['paper']['account']['next_hour_from'] = C   # a state without the keys yet
        self.github(st, L + 1, [], self.late_now())
        self.assertEqual((acc['exit_rules_from'], acc['trail_risk_from']), (C, C))
        # the fill at C (signal of C − 1 h) is under the old rules: no target (gene 0 = none before), no risk factor —
        # the same whether the server reads the state before (keys pending) or after GitHub stamped them
        self.assertEqual(self.replay(stale, L + 2), self.replay(st, L + 2))
        self.assertEqual(self.replay(stale, L + 3), self.replay(st, L + 3))
        p = self.replay(st, L + 3)['positions'][0]
        self.assertIsNone(p['target'])
        p = self.replay(st, L + 4)['positions'][0]                                   # from bar C + 1 h on: new exits
        self.assertIsNotNone(p['target'])

    # --- compatibility / safety -----------------------------------------------------------------------------------------------
    def test_old_state_replays_unchanged_and_without_now_keeps_the_old_stamps(self):
        L = self.L
        self.entries = {11: {k: [(self.COINS[k % 6], 1 if k % 2 else -1, 5.0)] for k in range(2, L + 5)}}
        old = self.state(team=('n1',))
        ref = json.loads(json.dumps(old)); v0, s0 = P.update(ref, self.sr(L + 5), [])            # master behaviour
        new = json.loads(json.dumps(old)); v1, s1 = P.update(new, self.sr(L + 5), [], now=self.ts(L + 4) + 30 * 60_000)
        self.assertEqual((v1['trades'], v1['curve'], v1['positions'], s1), (v0['trades'], v0['curve'], v0['positions'], s0))
        self.assertEqual(new['paper']['account']['segs'], ref['paper']['account']['segs'])
        self.assertNotIn('next_hour_from', ref['paper']['account'])
        # without `now` a change is stamped on the run's last bar, as before
        leg = json.loads(json.dumps(old)); leg['team'] = [dict(id='n2', since=0)]
        P.update(leg, self.sr(L + 1), [(0, 'n1', 'n2', 'challenger')])
        self.assertEqual(leg['paper']['account']['segs'][1]['from_ts'], self.ts(L))

    def test_old_stamps_keep_their_bar_mapping_new_ones_round_up(self):
        L = self.L
        sr = self.sr(L + 3)
        gap = {k: X.Series(k, v.ts[:10] + v.ts[11:], v.o[:10] + v.o[11:], v.h[:10] + v.h[11:], v.l[:10] + v.l[11:], v.c[:10] + v.c[11:]) for k, v in sr.items()}
        self.assertEqual(P._index(gap, self.ts(10)), 9)                             # an off-grid stamp: the bar before (old)
        self.entries = {11: {9: [('A-USDT-SWAP', 1, 5.0)]}, 12: {9: [('B-USDT-SWAP', 1, 5.0)]}}
        st = self.state(team=('n1',))
        st['paper']['account']['segs'][0]['wind_ts'] = self.ts(10)
        st['paper']['account']['segs'].append(dict(id='a2', champion='n2', slot=0, genes=fix_genes(st['nodes']['n2']['genes']), species='momentum',
                                                   from_ts=self.ts(10), wind_ts=None, boosts=[], reason='challenger'))
        st['team'] = [dict(id='n2', since=0)]
        with mock.patch.object(P, 'simulate_account', wraps=X.simulate_account) as sim:
            P.update(json.loads(json.dumps(st)), gap, [])
            self.assertEqual([s['from_i'] for s in sim.call_args[0][0]], [0, 9])     # before next_hour_from: floor
            st['paper']['account']['next_hour_from'] = self.ts(10)
            P.update(json.loads(json.dumps(st)), gap, [], replay=True)
            self.assertEqual([s['from_i'] for s in sim.call_args[0][0]], [0, 10])    # from next_hour_from on: the first bar at or after

    def test_replay_refuses_a_state_that_needs_a_github_stamp(self):
        st = self.state(team=('n1',))
        with self.assertRaises(P.StateDrift):                                        # predates the rule
            P.update(json.loads(json.dumps(st)), self.sr(self.L + 1), [], replay=True)
        self.github(st, self.L + 1, [], self.late_now())
        self.replay(st, self.L + 1)                                                  # agrees: fine
        drift = json.loads(json.dumps(st)); drift['team'] = [dict(id='n2', since=0)]
        with self.assertRaises(P.StateDrift):
            P.update(drift, self.sr(self.L + 1), [], replay=True)
        drift = json.loads(json.dumps(st)); drift['leader'] = 'n1'
        with self.assertRaises(P.StateDrift):
            P.update(drift, self.sr(self.L + 1), [], replay=True)

    def test_replay_is_deterministic(self):
        L = self.L
        self.entries = {11: {k: [(self.COINS[k % 6], 1 if k % 2 else -1, 3.0)] for k in range(2, L + 6)}}
        st = self.state(team=('n1', 'n2'), leader='n1'); self.github(st, L + 1, [], self.late_now())
        self.assertEqual(self.replay(st, L + 6), self.replay(st, L + 6))


def unranked(r):
    return dict(r, signals={k: {f: v for f, v in x.items() if f != 'rank'} for k, x in r['signals'].items()})


def fix_genes(g):
    return G.fix(dict(g))
