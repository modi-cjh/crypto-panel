"""Reserved short-timeframe team seats (evolution.TEAM_SHORT_TF_SEATS, tf in SHORT_TFS)."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'evolve'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import evolution as E   # noqa: E402
import team_scenarios as S   # noqa: E402

H, T0 = S.H, S.T0
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'team_selection_seats0.json'


def make(specs):
    """specs: [(id, fitness, tf, species, direction, qualified)] → state with every strategy in the population."""
    nodes = {}
    for nid, fit, tf, sp, d, ok in specs:
        nodes[nid] = dict(id=nid, species=sp, fitness=fit, qualified=ok, genes=dict(direction=d, tf=tf),
                          test=dict(ret=10.0, max_dd=10.0, trades=10), final=dict(ret=5.0, max_dd=10.0, trades=8), oos_dd=10.0,
                          parents=[], born=0)
    st = dict(nodes=nodes, population=list(nodes), hall=[], streaks={}, champion=None, generation=0,
              defense_streaks={nid: 9 for nid in nodes})
    E.update_hall(st)
    return st


SPECIES = S.SPECIES
# eight strong daily strategies (two per species over four species), then weaker 4H / 1H ones
DAILY = [(f'd{k}', 5.0 - k * .1, 24, SPECIES[k // 2], 'both', True) for k in range(8)]
SHORT = [('h4a', 2.0, 4, SPECIES[4], 'both', True), ('h1b', 1.5, 1, SPECIES[5], 'both', True), ('h4c', 1.0, 4, SPECIES[4], 'both', True)]


def team_ids(st):
    return [m['id'] for m in st['team']]


def short_n(st):
    return sum(st['nodes'][i]['genes']['tf'] in E.SHORT_TFS for i in team_ids(st))


class ShortTimeframeSeatTests(unittest.TestCase):
    def setUp(self):
        for k in ('TEAM_SIZE', 'TEAM_SPECIES_CAP', 'TEAM_DIRECTION_CAP', 'TEAM_SHORT_TF_SEATS', 'SHORT_TFS'):
            self.addCleanup(setattr, E, k, getattr(E, k))
        E.TEAM_SIZE, E.TEAM_SPECIES_CAP, E.TEAM_DIRECTION_CAP, E.TEAM_SHORT_TF_SEATS = 8, 2, 5, 2

    def test_defaults(self):
        self.assertEqual(E.TEAM_SHORT_TF_SEATS, 2); self.assertEqual(tuple(E.SHORT_TFS), (1, 4)); self.assertEqual(E.TEAM_SIZE, 8)

    def test_zero_seats_reproduces_the_original_selection_exactly(self):
        """Golden output of the pre-reservation choose_team (origin/master 7dabb1b) on 13 seeded 40-run scenarios."""
        E.TEAM_SHORT_TF_SEATS = 0
        want = json.loads(FIXTURE.read_text())
        self.assertEqual(len(want['seeds']), 13)
        for key, expected in want['seeds'].items():
            got = S.run(E, int(key.lstrip('b')), biased=key.startswith('b'))
            self.assertEqual(json.loads(json.dumps(got)), expected, key)
        reasons = {c[3] for v in want['seeds'].values() for r in v for c in r['changes']}
        self.assertTrue({'first', 'breakdown', 'entry_gate', 'challenger', 'balance'} <= reasons)   # the paths covered

    def test_seats_are_taken_from_the_weakest_daily_members_by_the_best_short_tf_strategies(self):
        st = make(DAILY + SHORT)
        changes = E.choose_team(st, T0)
        team = team_ids(st)
        self.assertEqual(len(team), 8)                                         # the team does not grow
        self.assertEqual([(c[1], c[2], c[3]) for c in changes if c[3] == 'short_tf'], [('d7', 'h4a', 'short_tf'), ('d6', 'h1b', 'short_tf')])
        self.assertEqual(set(team), {'d0', 'd1', 'd2', 'd3', 'd4', 'd5', 'h4a', 'h1b'})
        self.assertNotIn('h4c', team)                                          # only as many as reserved
        self.assertEqual(E.choose_team(st, T0 + H), [])                        # stable on the next run
        self.assertEqual(E.choose_team(st, T0 + E.MIN_TENURE + H), [])         # and after tenure (no fitter challenger)

    def test_short_members_already_seated_count_and_a_full_reservation_changes_nothing(self):
        st = make(DAILY + SHORT)
        st['team'] = [dict(id=i, since=T0) for i in ['d0', 'd1', 'd2', 'd3', 'd4', 'd5', 'd6', 'h1b']]
        changes = E.choose_team(st, T0 + H)
        self.assertEqual([(c[1], c[2], c[3]) for c in changes], [('d6', 'h4a', 'short_tf')])   # one more, not two
        st = make(DAILY + SHORT)
        st['team'] = [dict(id=i, since=T0) for i in ['d0', 'd1', 'd2', 'd3', 'd4', 'h4a', 'h1b', 'h4c']]   # 3 short ≥ 2
        before = copy.deepcopy(st)
        self.assertEqual(E.choose_team(st, T0 + H), [])
        E.TEAM_SHORT_TF_SEATS = 0
        self.assertEqual(E.choose_team(before, T0 + H), [])
        self.assertEqual(team_ids(st), team_ids(before))

    def test_fewer_qualified_short_strategies_than_seats(self):
        one = [SHORT[0], (*SHORT[1][:5], False), (*SHORT[2][:5], False)]       # only h4a qualified
        st = make(DAILY + one)
        E.choose_team(st, T0)
        self.assertEqual(len(st['team']), 8); self.assertEqual(short_n(st), 1); self.assertIn('h4a', team_ids(st))
        self.assertEqual(E.choose_team(st, T0 + H), [])
        none = [(*s[:5], False) for s in SHORT]                                # none qualified: as before
        st, old = make(DAILY + none), make(DAILY + none)
        E.choose_team(st, T0)
        E.TEAM_SHORT_TF_SEATS = 0
        E.choose_team(old, T0)
        self.assertEqual(team_ids(st), team_ids(old)); self.assertEqual(len(st['team']), 8)
        E.TEAM_SHORT_TF_SEATS = 2                                              # nothing qualified at all: empty, no error
        st = make([(*s[:5], False) for s in DAILY + SHORT])
        self.assertEqual(E.choose_team(st, T0), []); self.assertEqual(st['team'], [])

    def test_a_reserved_member_that_leaves_is_replaced_by_a_short_strategy_when_one_is_free(self):
        st = make(DAILY + SHORT + [('d8', 4.5, 24, SPECIES[5], 'both', True)])
        st['team'] = [dict(id=i, since=T0) for i in ['d0', 'd1', 'd2', 'd3', 'd4', 'd5', 'h4a', 'h1b']]
        st['nodes']['h4a']['test']['ret'] = -7.0; st['nodes']['h4a']['qualified'] = False; E.update_hall(st)
        changes = E.choose_team(st, T0 + H)
        self.assertEqual([(c[1], c[2]) for c in changes], [('h4a', 'h4c')])   # not the fitter daily d8
        self.assertEqual(short_n(st), 2)
        # no free short strategy: the seat still gets filled (team never breaks), and is not swapped back and forth
        st['nodes']['h4c']['test']['ret'] = -7.0; st['nodes']['h4c']['qualified'] = False; E.update_hall(st)
        changes = E.choose_team(st, T0 + 2 * H)
        self.assertEqual([(c[1], c[2], c[3]) for c in changes], [('h4c', 'd8', 'breakdown')])
        self.assertEqual(len(st['team']), 8); self.assertEqual(short_n(st), 1)
        self.assertEqual(E.choose_team(st, T0 + 3 * H), [])

    def test_challenger_after_tenure_does_not_take_a_reserved_seat_with_a_daily_strategy(self):
        st = make(DAILY + SHORT)
        E.choose_team(st, T0)                                                  # h4a, h1b hold the reserved seats
        st['nodes']['d8'] = dict(st['nodes']['d0'], id='d8', fitness=9.0, species=SPECIES[5], genes=dict(direction='both', tf=24))
        st['population'].append('d8'); st['defense_streaks']['d8'] = 9
        E.update_hall(st)
        for k in range(1, 4):
            self.assertEqual(E.choose_team(st, T0 + k * H), [])                # tenure first; d8 builds its STREAK
        changes = E.choose_team(st, T0 + E.MIN_TENURE + 5 * H)
        # the weakest seat (h1b, 1.5) is reserved and no short challenger beats it: d8 takes the weakest other seat
        self.assertEqual([(c[1], c[2], c[3]) for c in changes], [('d5', 'd8', 'challenger')])
        self.assertEqual(short_n(st), 2)
        st['nodes']['h4c']['fitness'] = 1.6; E.update_hall(st)                  # a fitter short strategy may take a reserved seat
        changes = E.choose_team(st, T0 + E.MIN_TENURE + 6 * H)
        self.assertEqual([(c[1], c[2], c[3]) for c in changes], [('h1b', 'h4c', 'challenger')])
        self.assertEqual(short_n(st), 2)

    def test_reserve_never_displaces_the_leader_and_is_deterministic(self):
        st = make(DAILY + SHORT)
        st['team'] = [dict(id=i, since=T0) for i in ['d0', 'd1', 'd2', 'd3', 'd4', 'd5', 'd6', 'd7']]
        st['leader'] = st['champion'] = 'd7'                                     # weakest daily holds the title
        twin = copy.deepcopy(st)
        changes = E.choose_team(st, T0 + H)
        self.assertEqual([c[1] for c in changes], ['d6', 'd5'])
        self.assertIn('d7', team_ids(st))
        self.assertEqual(E.choose_team(twin, T0 + H), changes); self.assertEqual(twin['team'], st['team'])

    def test_reserve_respects_the_species_cap_when_it_can_like_an_empty_seat(self):
        short = [('h4x', 3.0, 4, SPECIES[0], 'both', True), ('h4y', 2.5, 4, SPECIES[0], 'both', True), ('h1z', 1.0, 1, SPECIES[5], 'both', True)]
        st = make(DAILY + short)                                               # momentum (SPECIES[0]) already has d0, d1
        changes = [c for c in E.choose_team(st, T0) if c[3] == 'short_tf']
        # first seat: the best short strategy the species cap allows; second: no capped one left, so the cap gives way
        # (as for an empty seat) rather than leave the reservation unfilled
        self.assertEqual([(c[1], c[2]) for c in changes], [('d7', 'h1z'), ('d6', 'h4x')])
        self.assertEqual(len(st['team']), 8)

    def test_random_scenarios_keep_the_reservation_whenever_short_strategies_are_free_and_are_deterministic(self):
        want = json.loads(FIXTURE.read_text())['seeds']
        for key in want:
            seed, biased = int(key.lstrip('b')), key.startswith('b')
            got = S.run(E, seed, biased=biased)
            self.assertEqual(got, S.run(E, seed, biased=biased), key)
            for k, (new, old) in enumerate(zip(got, want[key])):
                self.assertEqual(len(new['team']), len(old['team']), (key, k))              # never fewer seats than before
                self.assertEqual(len({i for i, _ in new['team']}), len(new['team']))       # no duplicates
            seen = []                                                                       # replay to check the invariant
            orig = E.choose_team
            def spy(state, now_ms):
                out = orig(state, now_ms)
                n = state['nodes']
                tm = {m['id'] for m in state['team']}
                free = [i for i in state['hall'] if i not in tm and n[i]['qualified'] and n[i]['genes']['tf'] in E.SHORT_TFS]
                seen.append(sum(n[i]['genes']['tf'] in E.SHORT_TFS for i in tm) >= E.TEAM_SHORT_TF_SEATS or not free)
                return out
            E.choose_team = spy
            try:
                S.run(E, seed, biased=biased)
            finally:
                E.choose_team = orig
            self.assertTrue(all(seen), key)

    def test_dashboard_rules_expose_the_seat_count(self):
        import run as R
        src = Path(R.__file__).read_text()
        self.assertIn('short_tf_seats=E.TEAM_SHORT_TF_SEATS', src); self.assertIn('short_tfs=list(E.SHORT_TFS)', src)


if __name__ == '__main__':
    unittest.main()
