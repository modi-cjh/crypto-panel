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
    g.update(direction='both', lookback=20, stop_atr=2.0, target_rr=0.0, max_hold=240, risk=.01, vol_min=.3, vol_max=4.0)
    g.update(kw)
    return g


class ExecutionTests(unittest.TestCase):
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
        gap = series_from(closes + [80.0], opens=[closes[0]] + closes[:-1] + [80.0])   # opens below the stop
        r3 = X.simulate(g, {'BTC-USDT-SWAP': gap}, 30, gap.n)
        self.assertEqual(r3['trades'][-1]['reason'], 'stop'); self.assertAlmostEqual(r3['trades'][-1]['exit'], 80.0 * (1 - X.SLIP))

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
        self.assertEqual(sum(1 for n in fixed if n['how'] == 'niche'), 9)
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
            nodes[nid].update(fitness=fit, qualified=ok, test=dict(nodes[nid]['test'], ret=ret, max_dd=dd))
        for nid in st['population'][len(fits):]:
            nodes[nid].update(fitness=0.0, qualified=False)
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
        changes = E.choose_team(st, T0 + 25 * h)                           # one swap only, against the weakest (1.0)
        self.assertEqual(len(changes), 1); self.assertEqual(changes[0][1], ids[4]); self.assertEqual(changes[0][3], 'challenger')
        self.assertIn(changes[0][2], ids[5:])
        changes = E.choose_team(st, T0 + 26 * h)                           # next run: the next-weakest seasoned member (2.0)
        self.assertEqual(len(changes), 1); self.assertEqual(changes[0][1], ids[3])

    def test_breakdown_replaces_a_member_at_once_and_migration_keeps_the_old_champion(self):
        st, ids = self._setup([(5.0, True, 3, 10), (4.0, True, 3, 10), (3.0, True, 3, 10)])
        for k, nid in enumerate(ids): st['nodes'][nid]['species'] = G.SPECIES[k]
        E.update_hall(st)
        st['champion'], st['champion_since'] = ids[1], T0 - 5
        st.pop('team', None)
        E.choose_team(st, T0)
        self.assertEqual(st['team'][0], dict(id=ids[1], since=T0 - 5))        # migrated with its tenure
        st['nodes'][ids[1]]['test'].update(ret=-3.0); st['nodes'][ids[1]]['qualified'] = False; E.update_hall(st)
        st['nodes'][st['population'][3]].update(fitness=2.0, qualified=True, species='trend'); E.update_hall(st)
        changes = E.choose_team(st, T0 + 3600_000)
        self.assertIn((0, ids[1], st['population'][3], 'breakdown'), changes)
        self.assertFalse(E.breaks_retain(dict(test=dict(ret=0, max_dd=29))))
        self.assertTrue(E.breaks_retain(dict(test=dict(ret=5, max_dd=31))))

    def test_team_paper_slots_sum_and_mirror_signals_do_not_collide(self):
        st = E.init_state(5, self.sr, self.split)
        E.choose_team(st, self.sr['BTC-USDT-SWAP'].ts[500])
        changes = [(i, None, m['id'], 'first') for i, m in enumerate(st['team'])]
        # start the slots some bars back so they have something to trade
        view, signals = P.update(st, {k: v for k, v in self.sr.items()}, changes)
        self.assertEqual(len(st['paper']['slots']), E.TEAM_SIZE)
        self.assertAlmostEqual(view['equity'], sum(m['equity'] for m in view['members']) + 2000 * (E.TEAM_SIZE - len(view['members'])), places=1)
        self.assertEqual(len(signals), len({p['inst'] for p in view['positions']} | {p['inst'] for p in view['pending']}))
        for inst, sig in signals.items(): self.assertIn(sig['champion'], [m['id'] for m in st['team']]); self.assertIn(sig['rank'], range(1, len(st['team']) + 1))
        seg = st['paper']['slots'][0]['segments'][-1]                      # each slot is the backtest engine on live bars
        self.assertEqual(P.run_segment(seg, seg['genes'], self.sr)['equity'], view['members'][[m['slot'] for m in view['members']].index(0)]['equity'])

    def test_prune_keeps_champion_ancestry(self):
        st = E.init_state(3, self.sr, self.split)
        for _ in range(3): E.generation(st, self.sr, self.split, self.obs)
        champ = next(n for n in st['nodes'].values() if n['parents'])
        st['champion'] = champ['id']; st['generation'] = 50
        E.prune(st)
        self.assertIn(champ['id'], st['nodes'])
        for p in champ['parents']: self.assertIn(p, st['nodes'])


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

    def test_split_is_seventy_thirty_after_warmup(self):
        sr = tiny_market(1250)
        start, cut, end = D.split(sr)
        self.assertEqual(start, 125); self.assertEqual(cut - start, int((end - start) * .7))


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
            dmod.load_series.__defaults__ = (get, D.INSTRUMENTS)
            with tempfile.TemporaryDirectory() as tmp:
                R.main(['--data', tmp, '--generations', '2'])
                out = Path(tmp) / 'evolve'
                dash = json.loads((out / 'dashboard.json').read_text())
                self.assertEqual(dash['generation'], 2); self.assertEqual(len(dash['history']), 2)
                self.assertTrue(dash['nodes']); self.assertEqual(dash['data']['train_share'], .7)
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
            dmod.load_series.__defaults__ = (orig, D.INSTRUMENTS)


if __name__ == '__main__':
    unittest.main()
