import json
import math
import random
import sys
import tempfile
import unittest
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
    g.update(direction='both', lookback=20, stop_atr=2.0, target_rr=0.0, max_hold=240, risk=.02, vol_min=.3, vol_max=4.0, tf=1, trail=0)
    g.update(kw)
    return g


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
        crash = series_from(closes + [80.0], opens=[closes[0]] + closes[:-1] + [80.0])    # −27 %: through the liquidation price
        r4 = X.simulate(g, {'BTC-USDT-SWAP': crash}, 30, crash.n)
        t = r4['trades'][-1]
        self.assertEqual(t['reason'], 'liquidation')
        self.assertAlmostEqual(t['pnl'] + p['qty'] * t['exit'] * X.FEE + p['qty'] * p['entry'] * X.FEE, -p['margin'], places=3)   # loses exactly the margin (+ fees)

    def test_contract_mode_lots_leverage_margin_and_funding(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 30
        s = series_from(closes)
        g = base_genes(risk=.05)
        free = X.simulate(g, {'BTC-USDT-SWAP': s}, 30, len(closes))['positions'][0]
        s2 = series_from(closes); s2.spec = (0.1, 1.0, 1.0)                     # 0.1 coin per contract, whole contracts
        p = X.simulate(g, {'BTC-USDT-SWAP': s2}, 30, len(closes))['positions'][0]
        self.assertAlmostEqual(p['qty'] / .1, round(p['qty'] / .1)); self.assertLessEqual(p['qty'], free['qty'])
        dist = (p['entry'] - p['stop']) / p['entry']
        self.assertEqual(p['lever'], max(1, min(X.LEVERAGE, int(.8 / dist))))
        self.assertAlmostEqual(p['margin'], p['qty'] * p['entry'] / p['lever'], places=3)
        self.assertAlmostEqual(p['liq'], p['entry'] * (1 - (1 / p['lever'] - X.MMR)))
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

    def test_time_exit_and_metrics(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 10
        s = series_from(closes)
        r = X.simulate(base_genes(max_hold=4), {'BTC-USDT-SWAP': s}, 30, s.n)
        self.assertEqual(r['trades'][0]['reason'], 'time')
        self.assertEqual(r['metrics']['trades'], len(r['trades']))
        self.assertGreaterEqual(r['metrics']['max_dd'], 0)

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
        self.assertIn('1 小时', G.describe(old))                   # dashboard text for genomes stored before the new genes
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
        closes = [100.0] * 40 + [110.0] + [112.0, 116.0, 120.0, 118.0]
        s = series_from(closes)
        fixed = X.simulate(base_genes(), {'BTC-USDT-SWAP': s}, 30, s.n)['positions'][0]
        trail = X.simulate(base_genes(trail=1), {'BTC-USDT-SWAP': s}, 30, s.n)['positions'][0]
        self.assertAlmostEqual(fixed['stop'], fixed['entry'] - X.stop_distance(base_genes(), s, 40))
        self.assertGreater(trail['stop'], fixed['stop'])
        self.assertAlmostEqual(trail['stop'], max(s.h[41:]) - (trail['entry'] - fixed['stop']))

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
            g = m['genes']
            for k, (kind, lo, hi) in G.GENES.items():
                if kind != 'choice': self.assertTrue(lo <= g[k] <= hi, (k, g[k]))
                else: self.assertIn(g[k], lo)


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
        return st, ids

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
            if any(t['reason'] == 'liquidation' for t in a['trades']):
                continue
            b = X.simulate_account([dict(id='x', genes=g, from_i=0, wind_i=None, boosts=[])], self.sr, 60, 900, equity=5000.0, rules=loose)
            self.assertAlmostEqual(a['equity'], b['equity'], places=6, msg=g)
            self.assertEqual(len(a['trades']), len(b['trades'])); self.assertEqual(a['curve'], b['curve'])
            self.assertAlmostEqual(b['pnl']['x'], b['equity'] - 5000.0, places=6)

    def test_shared_account_rules_one_position_per_coin_crowding_cap_leader_and_wind_down(self):
        sr = self._breakout_market()
        g = base_genes(risk=.02)
        seg = lambda i, **kw: dict(dict(id=f'm{i}', genes=g, from_i=0, wind_i=None, boosts=[]), **kw)
        r = X.simulate_account([seg(1), seg(2), seg(3)], sr, 30, 71, equity=10000.0)
        pos = {p['inst']: p for p in r['positions']}
        self.assertEqual(len(pos), 8)                                                    # one position per coin, 3 per strategy at most
        self.assertEqual([sum(1 for p in pos.values() if p['seg'] == f'm{i}') for i in (1, 2, 3)], [3, 3, 2])
        risk = [round(p['qty'] * (p['entry'] - p['stop']) / 10000.0, 3) for p in r['positions']]
        self.assertEqual(risk[:3], [.02, .02, .02])                                      # the first three longs: full size
        for x in risk[3:6]: self.assertAlmostEqual(x, .01, delta=.0005)                  # 3+ longs already open: half
        for x in risk[6:]: self.assertAlmostEqual(x, .005, delta=.0005)                  # 6+: a quarter
        self.assertEqual((X.crowd_factor(2), X.crowd_factor(3), X.crowd_factor(6)), (1.0, .5, .25))
        # same-side cap: 5 % each, the cap is 20 % → four fit, the rest is skipped (crowding halves from the 4th)
        g5 = base_genes(risk=.05)
        r5 = X.simulate_account([dict(id=f'm{i}', genes=g5, from_i=0, wind_i=None, boosts=[]) for i in range(4)], sr, 30, 71, equity=10000.0)
        total = sum(p['qty'] * (p['entry'] - p['stop']) for p in r5['positions']) / 10000.0
        self.assertLessEqual(total, .205); self.assertGreater(total, .17)       # (measured on the starting equity; fees move it a hair)
        # the leader: 20 % of the whole account per trade, leverage above the normal cap, outside the same-side cap
        lead = X.simulate_account([seg(1, boosts=[(0, None)]), seg(2, genes=g5)], sr, 30, 71, equity=10000.0)
        lp = [p for p in lead['positions'] if p['boost']]
        self.assertEqual(len(lp), 3)
        self.assertAlmostEqual(lp[0]['qty'] * (lp[0]['entry'] - lp[0]['stop']) / 10000.0, X.ACCOUNT_RULES['leader_risk'], delta=.002)
        others = [p for p in lead['positions'] if not p['boost']]
        self.assertTrue(others)                                                          # the leader's 60 % did not use up the others' cap
        self.assertAlmostEqual(others[0]['qty'] * (others[0]['entry'] - others[0]['stop']) / 10000.0, .025, delta=.003)   # 4th long: half of 5 %
        # wind-down: a segment that lost its seat at bar 45 keeps its positions but takes no new signal
        w = X.simulate_account([seg(1, wind_i=45)], sr, 30, 71, equity=10000.0)
        self.assertEqual(len(w['positions']), 3)
        self.assertEqual(X.simulate_account([seg(1, wind_i=40)], sr, 30, 71, equity=10000.0)['positions'], [])   # wound before the signal bar

    def test_majors_risk_twice_as_much_in_the_shared_account(self):
        closes = [100.0] * 40 + [110.0] + [111.0] * 30
        sr = {k: series_from(list(closes), k) for k in ('BTC-USDT-SWAP', 'PEPE-USDT-SWAP')}
        r = X.simulate_account([dict(id='m', genes=base_genes(risk=.02), from_i=0, wind_i=None, boosts=[])], sr, 30, 71, equity=10000.0)
        risk = {p['inst']: round(p['qty'] * (p['entry'] - p['stop']) / 10000.0, 3) for p in r['positions']}
        self.assertEqual(risk, {'BTC-USDT-SWAP': .04, 'PEPE-USDT-SWAP': .02})
        self.assertEqual(X.ACCOUNT_RULES['major_factor'], 2.0); self.assertIn('DOGE-USDT-SWAP', X.MAJORS)

    def test_daily_halt_closes_the_other_members_and_leaves_the_leader(self):
        closes = [100.0] * 40 + [110.0, 111.0, 111.0] + [104.0] * 20                     # breakout, then a fall that stays above the stops
        sr = {f'C{k}-USDT-SWAP': series_from(list(closes), f'C{k}-USDT-SWAP') for k in range(6)}
        g = base_genes(risk=.05, stop_atr=6.0)
        segs = [dict(id='lead', genes=base_genes(risk=.02, stop_atr=6.0), from_i=0, wind_i=None, boosts=[(0, None)])] + \
               [dict(id=f'm{i}', genes=g, from_i=0, wind_i=None, boosts=[]) for i in range(2)]
        r = X.simulate_account(segs, sr, 30, len(closes), equity=10000.0, rules=dict(X.ACCOUNT_RULES, daily_halt=.03))
        halted = [t for t in r['trades'] if t['reason'] == 'daily_halt']
        self.assertTrue(halted); self.assertFalse(any(t['boost'] for t in halted))       # only the other members are closed
        self.assertTrue(all(p['boost'] for p in r['positions'])); self.assertTrue(r['positions'])   # the leader's positions stay

    def test_paper_account_is_shared_converts_the_old_slots_and_follows_seat_and_leader_changes(self):
        st, ids = self._setup([(5.0, True, 3, 10), (4.0, True, 3, 10), (3.0, True, 3, 10), (2.0, True, 3, 10), (1.0, True, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % 4]
        E.update_hall(st); E.choose_team(st, T0)
        n = self.sr['BTC-USDT-SWAP'].n
        cut = lambda back: {k: X.Series(k, v.ts[:n - back], v.o[:n - back], v.h[:n - back], v.l[:n - back], v.c[:n - back]) for k, v in self.sr.items()}
        st['paper'] = dict(start_equity=10000.0, slots=[dict(P._new_slot(2100.0), curve_done=[[T0, 2000.0], [T0 + H, 2100.0]]) for _ in range(E.TEAM_SIZE)])
        view, signals = P.update(st, cut(300), [])
        acc = st['paper']['account']
        self.assertNotIn('slots', st['paper']); self.assertEqual(acc['start_equity'], 10500.0)        # the slots' total carries over
        self.assertEqual(view['equity'], 10500.0); self.assertEqual(view['mode'], 'cross')
        self.assertEqual(view['curve'][-2:], [[T0 + H, 10500.0], [cut(300)['BTC-USDT-SWAP'].ts[-1], 10500.0]])   # old curve kept, new one continues
        self.assertEqual([g['champion'] for g in acc['segs']], ids); self.assertEqual([m['id'] for m in view['members']], ids)
        self.assertEqual([bool(g['boosts']) for g in acc['segs']], [True, False, False, False, False])  # the leader's title
        view, signals = P.update(st, cut(150), [])                                                       # 150 bars later
        self.assertAlmostEqual(view['equity'], 10500.0 + sum(m['pnl'] for m in view['members']), places=1)   # one account: equity = start + everyone's result
        self.assertEqual(len({p['inst'] for p in view['positions']}), len(view['positions']))           # one position per coin
        for inst, sig in signals.items():
            lead = sig['champion'] == ids[0]
            self.assertEqual((sig.get('leader', False), sig['leverage']), (True, 50) if lead else (False, 10))
            if lead: self.assertEqual(sig['risk'], P.LEADER_RISK)
        # a seat changes hands: the old member winds down, the title moves
        new = next(nid for nid in st['population'] if nid not in ids)
        st['team'][1] = dict(id=new, since=0); st['leader'] = ids[2]
        view2, _ = P.update(st, cut(150), [(1, ids[1], new, 'challenger')])
        segs = {g['champion']: g for g in acc['segs']}
        last = cut(150)['BTC-USDT-SWAP'].ts[-1]
        self.assertEqual(segs[ids[1]]['wind_ts'], last); self.assertIsNone(segs[new]['wind_ts'])
        self.assertEqual(segs[ids[0]]['boosts'], [[cut(300)['BTC-USDT-SWAP'].ts[-1], last]]); self.assertEqual(segs[ids[2]]['boosts'], [[last, None]])
        self.assertAlmostEqual(view2['equity'], view['equity'], places=2)                               # nothing is sold by the change itself
        self.assertEqual(view2['leader']['id'], ids[2]); self.assertEqual(view2['members'][0]['id'], ids[2])
        self.assertIn('leader', [x['reason'] for x in view2['segments']]); self.assertIn('challenger', [x['reason'] for x in view2['segments']])
        view3, _ = P.update(st, self.sr, [])
        self.assertFalse([t for t in view3['trades'] if t['reason'] == 'switch'])

    def test_leader_title_is_sticky(self):
        st, ids = self._setup([(5.0, True, 3, 10), (4.0, True, 3, 10), (3.0, True, 3, 10), (2.0, True, 3, 10), (1.0, True, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k % 4]
        E.update_hall(st); E.choose_team(st, T0)
        self.assertEqual(st['champion'], ids[0])
        st['nodes'][ids[1]]['fitness'] = 9.0
        E.choose_team(st, T0 + 3600_000)
        self.assertEqual(st['champion'], ids[0])                                      # better fitness, but tenure has not passed
        E.choose_team(st, T0 + E.MIN_TENURE + 3600_000)
        self.assertEqual(st['champion'], ids[1])
        # the bigger size is its own title: only for a member that passed both gates
        self.assertEqual(st['leader'], ids[1]); self.assertEqual(P.LEADER_RISK, .05)
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
                seated = {m['id'] for m in dash['team']} | {dash['champion']['id']}
                self.assertEqual(dash['schema'], 1); self.assertEqual(dash['details'], 'nodes.json')   # schema 1: pages from before nodes.json still read it
                for n in dash['nodes']: self.assertIn('genes', n); self.assertEqual('summary' in n, n['id'] in seated); self.assertIn('train', n)
                det = json.loads((out / 'nodes.json').read_text())
                self.assertEqual(det['generation'], 2); self.assertEqual(set(det['nodes']), {n['id'] for n in dash['nodes']})
                self.assertTrue(all({'genes', 'summary', 'test_curve'} <= set(v) for v in det['nodes'].values()))
                sig = json.loads((out / 'signals.json').read_text())
                self.assertIn('positions', sig)
                for inst, p in sig['positions'].items():
                    self.assertIn(p['side'], (1, -1)); self.assertIn('stop', p); self.assertIn('opened', p)
                R.main(['--data', tmp, '--generations', '1'])            # second run resumes the same population
                self.assertEqual(json.loads((out / 'dashboard.json').read_text())['generation'], 3)
        finally:
            dmod.load_series.__defaults__ = (orig, None, None)


if __name__ == '__main__':
    unittest.main()
