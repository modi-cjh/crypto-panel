"""Deterministic team-selection scenarios for evolution.choose_team (no market data needed).

run(E, seed) drives E.update_hall + E.choose_team through a seeded sequence of runs (qualification flips, fitness
drift, breakdowns, tenure jumps) and returns what was picked each run. tests/fixtures/team_selection_seats0.json holds
the output of the pre-reservation choose_team (origin/master 7dabb1b) for a few seeds; with TEAM_SHORT_TF_SEATS = 0 the
current code must reproduce it exactly.
"""
import random

SPECIES = ['momentum', 'mean_revert', 'vol_breakout', 'trend', 'vol_surge', 'rel_strength']
H = 3600_000
T0 = 1_790_000_000_000


def node(nid, rng, biased=False):
    ret, dd = round(rng.uniform(-8, 40), 3), round(rng.uniform(3, 35), 3)
    return dict(id=nid, species=rng.choice(SPECIES), fitness=round(rng.uniform(-1, 5), 4), qualified=rng.random() < .55,
                genes=dict(direction='short' if biased and rng.random() < .8 else rng.choice(['both', 'long', 'short']), tf=rng.choice([1, 4, 24])),
                test=dict(ret=ret, max_dd=dd, trades=rng.randint(4, 30)), final=dict(ret=round(rng.uniform(-6, 30), 3), max_dd=dd, trades=8),
                oos_dd=dd, parents=[], born=0)


def build(seed, n=60, biased=False):
    rng = random.Random(seed)
    nodes = {f's{k}': node(f's{k}', rng, biased) for k in range(n)}
    st = dict(nodes=nodes, population=list(nodes), hall=[], streaks={}, champion=None, generation=0,
              defense_streaks={nid: rng.randint(0, 8) for nid in nodes})
    return st, rng


def run(E, seed, runs=40, biased=False):
    """biased: mostly short-only strategies and calmer runs, starting from an over-quota
    all-short team, so the direction 'balance' path occurs."""
    st, rng = build(seed, biased=biased)
    nodes, now, out = st['nodes'], T0, []
    if biased:                                                       # a seated team from before the direction quota: 8 shorts
        st['team'] = [dict(id=nid, since=T0) for nid in [i for i in nodes if nodes[i]['genes']['direction'] == 'short'][:8]]
        for m in st['team']:                                         # healthy members: only the quota moves them
            nodes[m['id']].update(qualified=True, oos_dd=10.0); nodes[m['id']]['test'].update(ret=12.0, max_dd=10.0)
            nodes[m['id']]['final'].update(ret=8.0, max_dd=10.0)
    for k in range(runs):
        seated = {m['id'] for m in st.get('team', [])} if biased else set()
        for nid in rng.sample(list(nodes), 2 if biased else 6):      # some strategies start / stop qualifying
            if nid not in seated:
                nodes[nid]['qualified'] = rng.random() < .6
        for nid in rng.sample(list(nodes), 6):
            nodes[nid]['fitness'] = round(nodes[nid]['fitness'] + rng.uniform(-.5, .5), 4)
        if not biased and rng.random() < .15:                        # a clear breakdown
            nid = rng.choice(list(nodes)); nodes[nid]['test']['ret'] = -7.0; nodes[nid]['qualified'] = False
        for nid in nodes:
            st['defense_streaks'][nid] = st['defense_streaks'][nid] + 1 if nodes[nid]['qualified'] else 0
        now += rng.choice([H, H, H, 2 * H, E.MIN_TENURE + H])
        E.update_hall(st)
        changes = E.choose_team(st, now)
        out.append(dict(changes=[list(c) for c in changes], team=[[m['id'], m['since']] for m in st['team']],
                        champion=st['champion'], leader=st['leader'], unqualified=st.get('unqualified', {})))
    return out
