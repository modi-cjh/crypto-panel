"""Engine layer: the generational loop.

Each generation: OBSERVE volatility → BREED (tournament selection, crossover, volatility-scaled mutation, a few random
immigrants) → TEST every child on the training window (fitness) and the unseen window (out-of-sample gate) → SELECT the
next population (elites survive, the rest are replaced) → DEPLOY: the qualified strategy with the best training fitness
becomes the paper-trading champion.
"""
import random

from execution import simulate, entries
from genome import random_genome, crossover, mutate, fix, SPECIES
import data as D

POPULATION, ELITES, TOURNAMENT, CROSSOVER, IMMIGRANTS = 60, 6, 3, .7, 3
MIN_PER_SPECIES = 3     # diversity floor: every species keeps at least this many members each generation
HALL_SIZE, KEEP_GENERATIONS, CURVE_POINTS = 20, 12, 40
HALL_PER_DIRECTION = 3   # besides the top HALL_SIZE, the hall keeps the best qualified long-only / short-only / both
GATE = dict(min_ret=0.0, max_dd=25.0, min_trades=8)   # out-of-sample requirements
# Final check: the newest part of the window is never used for fitness or breeding. 13,000 strategies were screened
# against one out-of-sample window, so passing it is partly luck; a strategy must also hold up here to be 'qualified'.
FINAL_GATE = dict(min_ret=0.0, max_dd=25.0, min_trades=4)
# Regime balance: training days are split by the market trend (BTC against 30 days earlier). A strategy that only makes
# money in rising or only in falling markets has its fitness halved, so a one-way training window (2025-11 → 2026-07
# fell) does not breed a population of pure short sellers.
REGIME_LOOKBACK_H, REGIME_MIN_DAYS, REGIME_FACTOR = 720, 20, .5
SWITCH_MARGIN = 1.02                                   # a challenger must beat the champion's fitness by 2 %
# Champion stability: the out-of-sample window moves one bar per hour, so strategies near the gate flip between
# qualified and not. The champion therefore keeps its seat for MIN_TENURE unless it clearly breaks down (RETAIN gate,
# looser than the entry GATE), and a challenger must have qualified in STREAK consecutive runs before it can take over.
MIN_TENURE = 7 * 24 * 3600_000     # a week: 38 of the first 52 paper closes were forced by seat changes, not by the strategies
STREAK = 3
RETAIN = dict(min_ret=-5.0, max_dd=30.0)
# The paper account is run by a team: the top TEAM_SIZE qualified strategies (at most TEAM_SPECIES_CAP per species), each
# with an equal share of capital, so one quiet strategy does not leave the whole account idle.
TEAM_SIZE, TEAM_SPECIES_CAP = 8, 2      # 8 seats (5 before): more trades a day, so the paper record says something sooner
# Direction quota: at most this many seats for long-only or for short-only strategies ('both' is not limited). The
# training window can be one long trend (2025-11 → 2026-07 fell 31–56 %), which breeds a population of short sellers;
# the quota keeps seats for qualified strategies of the other direction, so a trend reversal does not hit every seat.
TEAM_DIRECTION_CAP = 5
UNQUALIFIED_RUNS = 3      # a member that failed the gates this many runs in a row gives its seat to a qualified candidate
# Paper / OKX-demo entry gate (hard; never live). Each individual on the 8-seat team keeps its
# own defense streak in state['defense_streaks'][id]. A "defense" is one generation in which that
# individual is still on the team AND out-of-sample qualified (`qualified`, which includes the
# final window). The streak resets to 0 when the individual fails OOS or leaves the team.
# record_defense() runs only from generation(), so a refresh with no new generation does not add
# a count. Each Actions run breeds up to 5 generations, so a seated member that stays qualified
# can satisfy the 5-gen streak in a single run. The older single-champion fields
# state['defense_id'] / state['defense_streak'] are still written (and used to seed the map on
# old state). The drawdown check is on the combined unseen window (样本外 + 终审, costs included)
# and passes at max_dd <= 30 % (inclusive). GATE / FINAL_GATE (25 %) still decide `qualified`.
# The leader title still requires the entry gate, not just OOS qualification. The title decides
# signal priority; every seat, including the leader, sizes from its own risk and leverage genes.
ENTRY_STREAK = 5
ENTRY_MAX_DD = 30.0


def downsample(curve, n=CURVE_POINTS):
    if len(curve) <= n:
        return [list(p) for p in curve]
    step = (len(curve) - 1) / (n - 1)
    return [list(curve[round(i * step)]) for i in range(n)]


# Walk-forward fitness: the training window is cut into FOLDS time segments, scored separately from the same backtest.
# Later segments weigh more; the score is the weighted mean minus half the weighted spread, and segments that lose money
# cost extra, so a strategy that made all its money in one stretch ranks below one that earns steadily.
FOLDS = 4
FOLD_WEIGHTS = [.5 + i / (FOLDS - 1) for i in range(FOLDS)]      # 0.5 … 1.5
FOLD_CLIP = 10.0


def fold_scores(tr, ts, start, cut):
    """(score, ret %, max dd %, trades) per training segment, from the backtest's daily equity curve and trade list."""
    bounds = [start + (cut - start) * k // FOLDS for k in range(FOLDS + 1)]
    curve, out = tr['curve'], []
    for k in range(FOLDS):
        t0, t1 = ts[bounds[k]], ts[bounds[k + 1] - 1]
        before = [e for t, e in curve if t <= t0]
        pts = ([before[-1]] if before else [curve[0][1]]) + [e for t, e in curve if t0 < t <= t1]
        peak, dd = pts[0], 0.0
        for e in pts:
            peak = max(peak, e); dd = max(dd, (peak - e) / peak if peak > 0 else 0)
        ret = (pts[-1] / pts[0] - 1) * 100 if pts[0] > 0 else 0.0
        n = sum(1 for t in tr['trades'] if t0 < t['closed'] <= t1)
        score = max(-FOLD_CLIP, min(FOLD_CLIP, ret / max(dd * 100, 5.0)))
        out.append(dict(score=round(score, 3), ret=round(ret, 2), max_dd=round(dd * 100, 2), trades=n))
    return out


def robust_fitness(folds, trades):
    w = FOLD_WEIGHTS
    sc = [f['score'] for f in folds]
    mu = sum(a * b for a, b in zip(sc, w)) / sum(w)
    sd = (sum(b * (a - mu) ** 2 for a, b in zip(sc, w)) / sum(w)) ** .5
    losing = sum(1 for f in folds if f['ret'] <= 0)
    return round((mu - .5 * sd - .5 * max(0, losing - 1)) * min(1.0, trades / 30), 4)


def regime_returns(curve, ref):
    """Return (%) made on rising-market days and on falling-market days, from a daily equity curve. The market is
    'rising' at a time when ref (BTC) closed above its close REGIME_LOOKBACK_H hours earlier."""
    idx = ref._get(('tsidx',), lambda: {t: i for i, t in enumerate(ref.ts)})
    out = dict(up=0.0, down=0.0, up_days=0, down_days=0)
    for (t0, e0), (t1, e1) in zip(curve, curve[1:]):
        i = idx.get(t0)
        if i is None or e0 <= 0:
            continue
        k = 'up' if ref.c[i] > ref.c[max(0, i - REGIME_LOOKBACK_H)] else 'down'
        out[k] += (e1 / e0 - 1) * 100; out[k + '_days'] += 1
    out['up'], out['down'] = round(out['up'], 2), round(out['down'], 2)
    return out


def one_sided(reg):
    """True when both regimes had enough days and the strategy lost (or made nothing) in one of them."""
    return min(reg['up_days'], reg['down_days']) >= REGIME_MIN_DAYS and min(reg['up'], reg['down']) <= 0


def passes(m, gate):
    return m['ret'] > gate['min_ret'] and m['max_dd'] < gate['max_dd'] and m['trades'] >= gate['min_trades']


def _curve_dd(curves):
    """Max drawdown (%) of one or more equity curves stitched in order (the next curve is scaled onto the previous end)."""
    pts = []
    for curve in curves:
        if not curve:
            continue
        if not pts:
            pts = [tuple(p) for p in curve]
            continue
        base = curve[0][1]
        scale = pts[-1][1] / base if base else 1.0
        start = 1 if curve[0][0] == pts[-1][0] else 0
        pts += [(t, e * scale) for t, e in curve[start:]]
    if not pts:
        return 0.0
    peak, dd = pts[0][1], 0.0
    for _, e in pts:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak if peak > 0 else 0)
    return round(dd * 100, 3)


def oos_window_dd(node):
    """Max drawdown (%) on the unseen walk-forward window (out-of-sample + final, costs included).

    Uses the stored `oos_dd` when present; otherwise the worse of the two window metrics and the
    drawdown of the two curves stitched together (path-dependent combined DD can exceed either part).
    """
    if node.get('oos_dd') is not None:
        return float(node['oos_dd'])
    dds = []
    if node.get('test'):
        dds.append(node['test']['max_dd'])
    if node.get('final'):
        dds.append(node['final']['max_dd'])
    dds.append(_curve_dd([node.get('test_curve') or [], node.get('final_curve') or []]))
    return max(dds) if dds else 0.0


def update_defense_streak(prev_id, prev_streak, champ_id, qualified):
    """Legacy helper: consecutive generations the same individual has been the fitness champion
    AND out-of-sample qualified. Kept so older state and tests still round-trip. The paper entry
    gate now reads per-individual streaks from state['defense_streaks'] instead.
    """
    if not qualified:
        return 0
    return (prev_streak or 0) + 1 if champ_id == prev_id else 1


def migrate_defense_streaks(state):
    """Ensure state['defense_streaks'] exists. Old single-champion fields seed the map once."""
    if 'defense_streaks' not in state or state['defense_streaks'] is None:
        streaks = {}
        oid, n = state.get('defense_id'), int(state.get('defense_streak') or 0)
        if oid and n:
            streaks[oid] = n
        state['defense_streaks'] = streaks
    return state['defense_streaks']


def record_defense(state):
    """Once per generation (never from refresh): update each team member's defense streak.

    A defense = the individual is on the current 8-seat team and is out-of-sample qualified
    this generation. Qualified seated members increment; a fail or a drop from the team
    resets that id to 0. The legacy defense_id / defense_streak fields still follow the
    living population's fitness champion, so older readers keep working.
    """
    # migrate_defense_streaks may seed defense_streaks[defense_id] from the legacy
    # defense_streak, which already counts every completed generation. This function
    # is only called at the end of a *new* generation, so we then add exactly one
    # (not zero, not two) for a seated, still-qualified member. A prior
    # defense_streak_for() in the same run only reads the seeded map; it does not
    # increment, so the first record_defense after an upgrade cannot double-count.
    streaks = migrate_defense_streaks(state)
    nodes = state['nodes']
    seated = [m['id'] for m in state.get('team', []) if m.get('id') in nodes]
    seated_set = set(seated)
    for nid in list(streaks):
        if nid not in seated_set:
            streaks[nid] = 0
    for nid in seated:
        if nodes[nid].get('qualified'):
            streaks[nid] = int(streaks.get(nid, 0)) + 1
        else:
            streaks[nid] = 0
    state['defense_streaks'] = {nid: s for nid, s in streaks.items() if s or nid in seated_set}
    pop = [nodes[i] for i in state.get('population', []) if i in nodes]
    if not pop:
        return
    champ = max(pop, key=lambda n: n['fitness'])
    state['defense_streak'] = update_defense_streak(
        state.get('defense_id'), state.get('defense_streak', 0), champ['id'], bool(champ.get('qualified')))
    state['defense_id'] = champ['id']


def defense_streak_for(state, nid):
    if not nid or state is None:
        return 0
    streaks = migrate_defense_streaks(state)
    return int(streaks.get(nid, 0))


def entry_reasons(oos_ok, streak, dd, streak_need=ENTRY_STREAK, dd_lim=ENTRY_MAX_DD):
    reasons = []
    if not oos_ok:
        reasons.append('样本外（含成本的 walk-forward，含终审）未通过')
    if streak < streak_need:
        reasons.append(f'连续守擂 {streak}/{streak_need} 代，未满 {streak_need} 代')
    if dd > dd_lim:
        reasons.append(f'最大回撤 {dd}% 超过 {dd_lim:.0f}%')
    return reasons


def entry_gate(node, streak=None, state=None):
    """Three-part hard gate for joining the paper portfolio / OKX demo follow. Never live."""
    if streak is None:
        streak = defense_streak_for(state, node.get('id') if node else None) if state is not None else 0
    oos_ok = bool(node and node.get('qualified'))
    dd = oos_window_dd(node) if node else 0.0
    reasons = entry_reasons(oos_ok, streak, dd)
    return dict(oos_ok=oos_ok, streak=int(streak or 0), streak_needed=ENTRY_STREAK,
                max_dd=dd, max_dd_limit=ENTRY_MAX_DD, passed=not reasons, reasons=reasons)


def evaluate(genes, series, split):
    """split = (start, cut, end) or (start, cut, mid, end): train [start, cut), out-of-sample [cut, mid), final check
    [mid, end). 'qualified' = passes the out-of-sample gate and, when there is a final window, the final gate too."""
    start, cut, mid, end = split if len(split) == 4 else (*split, split[-1])
    genes = fix(genes)
    ev = entries(genes, series)
    tr = simulate(genes, series, start, cut, curve_every=24, events=ev)
    te = simulate(genes, series, cut, mid, curve_every=24, events=ev)
    m = te['metrics']
    out = dict(test=m, test_ok=passes(m, GATE), test_curve=downsample(te['curve']))
    out['qualified'] = out['test_ok']
    if end > mid:
        fi = simulate(genes, series, mid, end, curve_every=24, events=ev)
        out.update(final=fi['metrics'], final_ok=passes(fi['metrics'], FINAL_GATE), final_curve=downsample(fi['curve']))
        out['qualified'] = out['test_ok'] and out['final_ok']
    folds = fold_scores(tr, next(iter(series.values())).ts, start, cut)
    fit = robust_fitness(folds, tr['metrics']['trades'])
    train = dict(tr['metrics'], folds=folds)
    ref = series.get(D.REF)
    if ref is not None:
        train['regimes'] = reg = regime_returns(tr['curve'], ref)
        if one_sided(reg):
            fit = round(fit * REGIME_FACTOR if fit > 0 else fit * (2 - REGIME_FACTOR), 4)
    # Combined unseen-window DD (样本外 + 终审). Prefer the official per-window metrics (every close)
    # and also the stitched curves; path-dependent combined DD can exceed either part.
    out['oos_dd'] = max(
        [out['test']['max_dd']]
        + ([out['final']['max_dd']] if out.get('final') else [])
        + [_curve_dd([out.get('test_curve') or [], out.get('final_curve') or []])]
    )
    return dict(out, fitness=fit, train=train, train_curve=downsample(tr['curve']))


def add_migrants(state, migrants, series, split):
    """One-time import of strategies from the old rolling-evolution system: each joins the population as a 'migrant'
    in place of the weakest member that is not on the team, then competes like any other strategy."""
    if state.get('migrated') == migrants.get('version'):
        return []
    team = {m['id'] for m in state.get('team', [])}
    added = []
    for m in migrants['strategies']:
        node = new_node(state, fix(dict(m['genes'])), [], state['generation'], 'migrant', series, split)
        node['origin'] = m.get('origin')
        out = [nid for nid in state['population'] if nid not in team]
        weakest = min(out, key=lambda nid: state['nodes'][nid]['fitness'])
        state['population'].remove(weakest)
        state['nodes'][weakest]['died'] = state['generation']
        state['population'].append(node['id']); added.append(node['id'])
    state['migrated'] = migrants.get('version')
    update_hall(state)
    return added


def new_node(state, genes, parents, gen, how, series, split):
    state['next_id'] = state.get('next_id', 0) + 1
    node = dict(id=f"s{state['next_id']}", gen=gen, born=gen, died=None, species=genes['species'], parents=parents, how=how, genes=genes)
    node.update(evaluate(genes, series, split))
    state['nodes'][node['id']] = node
    state['stats']['born'] = state['stats'].get('born', 0) + 1
    return node


def init_state(seed, series, split):
    rng = random.Random(seed)
    state = dict(seed=seed, generation=0, nodes={}, population=[], hall=[], champion=None, history=[], stats=dict(born=0, killed=0), next_id=0,
                 defense_streaks={}, defense_id=None, defense_streak=0)
    for i in range(POPULATION):   # start with every species equally represented
        state['population'].append(new_node(state, random_genome(rng, SPECIES[i % len(SPECIES)]), [], 0, 'seed', series, split)['id'])
    update_hall(state)
    return state


def refresh(state, series, split):
    """The data window rolls forward every run: re-score the living population and the hall of fame on it.

    Does not call record_defense(): a refresh with no new generation must not increment any streak.
    """
    team = {m['id'] for m in state.get('team', [])}
    for nid in set(state['population']) | set(state['hall']) | team | ({state['champion']} if state['champion'] else set()):
        if nid in state['nodes']:
            n = state['nodes'][nid]
            n['genes'] = fix(n['genes'])
            n.update(evaluate(n['genes'], series, split))
    update_hall(state)


def shared_fitness(node, counts):
    """Fitness sharing for parent selection: a crowded species is discounted by √(its size), so one family cannot
    take over the whole population (premature convergence)."""
    k = max(1, counts.get(node['species'], 1)) ** .5
    return node['fitness'] / k if node['fitness'] > 0 else node['fitness'] * k


def tournament(pop, rng, counts=None):
    counts = counts or {}
    return max(rng.sample(pop, min(TOURNAMENT, len(pop))), key=lambda n: shared_fitness(n, counts))


def pick_elites(pop):
    """Species protection: the best of every species survives, then the remaining elite slots go by fitness."""
    ranked = sorted(pop, key=lambda n: n['fitness'], reverse=True)
    elites, seen = [], set()
    for n in ranked:
        if n['species'] not in seen:
            elites.append(n); seen.add(n['species'])
    for n in ranked:
        if len(elites) >= ELITES:
            break
        if n not in elites:
            elites.append(n)
    return sorted(elites, key=lambda n: n['fitness'], reverse=True)[:max(ELITES, len(seen))]


def update_hall(state):
    nodes = state['nodes']
    cand = {nid for nid in set(state['hall']) | set(state['population']) if nid in nodes and nodes[nid]['qualified']}
    ranked = sorted(cand, key=lambda nid: nodes[nid]['fitness'], reverse=True)
    hall = ranked[:HALL_SIZE]
    for d in ('long', 'short', 'both'):    # the best of each direction stay candidates for the team's direction quota
        hall += [nid for nid in ranked if nodes[nid]['genes'].get('direction', 'both') == d and nid not in hall][:HALL_PER_DIRECTION]
    state['hall'] = sorted(hall, key=lambda nid: nodes[nid]['fitness'], reverse=True)


def generation(state, series, split, obs):
    """Breed, test and select one generation. Returns the new generation's summary row."""
    gen = state['generation'] + 1
    rng = random.Random(f"{state['seed']}-{gen}")
    nodes = state['nodes']
    pop = [nodes[i] for i in state['population']]
    elites = pick_elites(pop)
    counts = {sp: sum(1 for n in pop if n['species'] == sp) for sp in SPECIES}
    children = []
    missing = [sp for sp in SPECIES if not counts[sp]]            # immigrants re-seed extinct species first
    for k in range(IMMIGRANTS):
        children.append(new_node(state, random_genome(rng, missing[k] if k < len(missing) else None), [], gen, 'immigrant', series, split))
    while len(children) < POPULATION - len(elites):
        a = tournament(pop, rng, counts)
        if rng.random() < CROSSOVER:
            b = tournament(pop, rng, counts)
            genes, parents, how = crossover(a['genes'], b['genes'], rng), [a['id'], b['id']], 'crossover'
        else:
            genes, parents, how = dict(a['genes']), [a['id']], 'mutation'
        genes = mutate(genes, rng, rate=obs['mutation_rate'])
        children.append(new_node(state, genes, parents, gen, how, series, split))
    children = protect_species(state, elites, children, pop, gen, rng, obs, series, split)
    new_pop = [n['id'] for n in elites] + [c['id'] for c in children]
    killed = [n['id'] for n in pop if n['id'] not in set(new_pop)]
    for nid in killed:
        nodes[nid]['died'] = gen
    state['stats']['killed'] = state['stats'].get('killed', 0) + len(killed)
    state['population'], state['generation'] = new_pop, gen
    update_hall(state)
    living = [nodes[i] for i in new_pop]
    best = max(living, key=lambda n: n['fitness'])
    row = dict(gen=gen, best=best['fitness'], mean=round(sum(n['fitness'] for n in living) / len(living), 4), best_id=best['id'],
               best_test_ret=best['test']['ret'], qualified=sum(1 for n in living if n['qualified']), born=len(children), killed=len(killed),
               regime=obs['regime'], vol=obs['market'], mutation_rate=obs['mutation_rate'],
               species={sp: sum(1 for n in living if n['species'] == sp) for sp in SPECIES})
    state['history'].append(row)
    record_defense(state)
    return row


def update_streaks(state):
    """Consecutive runs each hall-of-fame strategy has been out-of-sample qualified (call once per run)."""
    old = state.get('streaks', {})
    state['streaks'] = {nid: old.get(nid, 0) + 1 for nid in state['hall']}
    return state['streaks']


def breaks_retain(node):
    return any(t['ret'] < RETAIN['min_ret'] or t['max_dd'] > RETAIN['max_dd'] for t in (node['test'], node.get('final')) if t)


def protect_species(state, elites, children, pop, gen, rng, obs, series, split):
    """Diversity floor: a species below MIN_PER_SPECIES gets mutants of its best member (or fresh random members if it is
    extinct), replacing the weakest children of species that have more than enough."""
    nodes = state['nodes']
    def count(sp):
        return sum(1 for n in elites + children if n['species'] == sp)
    for sp in SPECIES:
        while count(sp) < MIN_PER_SPECIES:
            crowded = [c for c in children if count(c['species']) > MIN_PER_SPECIES]
            if not crowded:
                break
            weakest = min(crowded, key=lambda c: c['fitness'])
            children.remove(weakest)
            nodes.pop(weakest['id'], None); state['stats']['born'] -= 1     # never part of the generation
            kin = [n for n in pop + elites if n['species'] == sp]
            if kin:
                parent = max(kin, key=lambda n: n['fitness'])
                genes = mutate(dict(parent['genes'], species=sp), rng, rate=max(obs['mutation_rate'], .3))
                genes['species'] = sp
                children.append(new_node(state, genes, [parent['id']], gen, 'niche', series, split))
            else:
                children.append(new_node(state, random_genome(rng, sp), [], gen, 'niche', series, split))
    return children


def choose_team(state, now_ms):
    """Maintain the paper-trading team. Returns the changes made this run as [(slot, old_id, new_id, reason)].

    Each slot keeps its member for MIN_TENURE unless the member breaks the RETAIN gate, or cannot complete the
    paper entry gate (failed OOS, or combined unseen-window drawdown > ENTRY_MAX_DD). Those blocked seats can be
    given to a qualified challenger at once — tenure and the challenger STREAK do not apply — so a strategy that
    could pass the 5-gen gate is not stuck outside a full roster for a week. Members who are only short of the
    on-team 5-gen streak are left in place so they can finish accumulating. Empty slots are filled at once
    with the best qualified strategies. After tenure, a challenger that has qualified for STREAK consecutive runs and
    beats the weakest member's training fitness by SWITCH_MARGIN replaces it (at most one such swap per run).
    Seats prefer at most TEAM_SPECIES_CAP per species and at most TEAM_DIRECTION_CAP long-only / short-only members
    (only exceeded rather than leave a seat empty). When the team is over the direction quota and a qualified strategy of
    another direction exists, the weakest seasoned over-quota member is replaced ('balance'); that is this run's one swap.
    state['champion'] is the team leader (best training fitness).
    """
    nodes, streaks = state['nodes'], update_streaks(state)
    if 'team' not in state:                                    # migrate from the single-champion layout
        cur = state.get('champion')
        state['team'] = [dict(id=cur, since=state.get('champion_since', now_ms))] if cur in nodes else []
    team = [m for m in state['team'] if m['id'] in nodes]
    changes = []

    def species_ok(nid, replacing=None):
        sp = nodes[nid]['species']
        return sum(1 for m in team if m['id'] != replacing and nodes[m['id']]['species'] == sp) < TEAM_SPECIES_CAP

    direction = lambda nid: nodes[nid]['genes'].get('direction', 'both')

    def direction_count(d, replacing=None):
        return sum(1 for m in team if m['id'] != replacing and direction(m['id']) == d)

    def direction_ok(nid, replacing=None):
        d = direction(nid)
        return d == 'both' or direction_count(d, replacing) < TEAM_DIRECTION_CAP

    def candidates(pool, replacing=None):
        ids = {m['id'] for m in team}
        return [nid for nid in pool if nid not in ids and nid in nodes and species_ok(nid, replacing) and direction_ok(nid, replacing)]

    ready = [nid for nid in state['hall'] if streaks.get(nid, 0) >= STREAK]

    def blocked_from_gate(nid):
        n = nodes[nid]
        return (not n.get('qualified')) or oos_window_dd(n) > ENTRY_MAX_DD

    def qualified_challengers(replacing=None):
        gated, rest = [], []
        for nid in state['hall']:
            if nid not in nodes or not nodes[nid].get('qualified'):
                continue
            (gated if entry_gate(nodes[nid], state=state)['passed'] else rest).append(nid)
        return (candidates(gated, replacing) or candidates(ready, replacing) or candidates(rest, replacing))

    for slot, m in enumerate(team):                            # clear breakdowns are replaced at once
        if breaks_retain(nodes[m['id']]):
            pick = (candidates(ready, m['id']) or candidates(state['hall'], m['id']) or [None])[0]
            if pick:
                changes.append((slot, m['id'], pick, 'breakdown')); team[slot] = dict(id=pick, since=now_ms)
    # Seats that cannot finish the entry gate (failed OOS or DD > 30 %) are opened to a qualified
    # challenger at once. Tenure and STREAK=3 would otherwise keep a passing strategy off the
    # 8-seat roster, so it could never accumulate 5 on-team generations. Incumbents that are
    # still qualified with DD ≤ 30 % stay, even if their on-team streak is still below 5.
    changed_slots = {c[0] for c in changes}
    for slot, m in enumerate(team):
        if slot in changed_slots or not blocked_from_gate(m['id']):
            continue
        pick = (qualified_challengers(m['id']) or [None])[0]
        if pick:
            changes.append((slot, m['id'], pick, 'entry_gate')); team[slot] = dict(id=pick, since=now_ms)
    # Members that stopped qualifying: after UNQUALIFIED_RUNS runs in a row the seat goes to a qualified candidate (one per
    # run, tenure or fitness do not protect it). Without a candidate the member stays rather than leave the seat empty.
    # Usually entry_gate already made the swap; this remains a fallback when no challenger was free earlier.
    unq = state.get('unqualified', {})
    state['unqualified'] = unq = {m['id']: unq.get(m['id'], 0) + 1 for m in team if not nodes[m['id']].get('qualified')}
    if not changes:
        for slot, m in enumerate(team):
            if unq.get(m['id'], 0) >= UNQUALIFIED_RUNS:
                pick = (candidates(ready, m['id']) or [None])[0]
                if pick:
                    changes.append((slot, m['id'], pick, 'unqualified')); team[slot] = dict(id=pick, since=now_ms)
                    break
    while len(team) < TEAM_SIZE:                               # empty seats are filled with the best qualified;
        ids = {m['id'] for m in team}                           # the species cap is a preference, not a reason to leave a seat empty
        pick = (candidates(state['hall']) or [nid for nid in state['hall'] if nid not in ids and nid in nodes and direction_ok(nid)]
                or [nid for nid in state['hall'] if nid not in ids and nid in nodes] or [None])[0]
        if not pick:
            break
        team.append(dict(id=pick, since=now_ms)); changes.append((len(team) - 1, None, pick, 'first'))
    seasoned = [i for i, m in enumerate(team) if now_ms - m['since'] >= MIN_TENURE]
    over = [d for d in ('long', 'short') if direction_count(d) > TEAM_DIRECTION_CAP]
    if over and seasoned:                                      # over the direction quota: hand one seat to another direction
        slots = [i for i in seasoned if direction(team[i]['id']) == over[0]]
        if slots:
            slot = min(slots, key=lambda i: nodes[team[i]['id']]['fitness'])
            old = team[slot]['id']
            others = lambda pool: [nid for nid in candidates(pool, old) if direction(nid) != over[0]]
            pick = (others(ready) or others(state['hall']) or [None])[0]
            if pick:
                changes.append((slot, old, pick, 'balance')); team[slot] = dict(id=pick, since=now_ms)
                seasoned = []                                  # that was this run's one swap
    if seasoned:                                               # one challenger swap per run, against the weakest member
        slot = min(seasoned, key=lambda i: nodes[team[i]['id']]['fitness'])
        weakest = nodes[team[slot]['id']]
        for nid in candidates(ready, weakest['id']):   # direction_ok keeps a challenger from re-filling the quota
            if nodes[nid]['fitness'] > max(weakest['fitness'] * SWITCH_MARGIN, weakest['fitness'] + .01):
                changes.append((slot, weakest['id'], nid, 'challenger')); team[slot] = dict(id=nid, since=now_ms)
                break
    state['team'] = team
    # The leader (rank 1) has signal priority, so it must not flip between members from run
    # to run: it keeps the title while it is on the team, until MIN_TENURE has passed AND another member's fitness is
    # clearly higher (SWITCH_MARGIN).
    best = max(team, key=lambda m: nodes[m['id']]['fitness'], default=None)
    cur = next((m for m in team if m['id'] == state.get('champion')), None)
    since = state.get('leader_since') or (cur or {}).get('since') or now_ms
    if cur and best and best['id'] != cur['id']:
        fc, fb = nodes[cur['id']]['fitness'], nodes[best['id']]['fitness']
        if not (now_ms - since >= MIN_TENURE and fb > max(fc * SWITCH_MARGIN, fc + .01)):
            best = cur
    if best and (not cur or best['id'] != cur['id']):
        since = now_ms
    state['champion'] = best['id'] if best else None
    state['champion_since'] = best['since'] if best else None
    state['leader_since'] = since if best else None
    # The bigger size (paper.LEADER_RISK) is a separate title, state['leader']: only a member that passed the
    # three-part entry gate may hold it, it is lost at once when the holder stops passing, and it moves to
    # a better gated member only after MIN_TENURE and a clear lead. Nobody passed → nobody trades bigger.
    ok = [m for m in team if entry_gate(nodes[m['id']], state=state)['passed']]
    hold = next((m for m in ok if m['id'] == state.get('leader')), None)
    top = max(ok, key=lambda m: nodes[m['id']]['fitness'], default=None)
    boost_since = state.get('boost_since') or now_ms
    if hold and top['id'] != hold['id']:
        fh, ft = nodes[hold['id']]['fitness'], nodes[top['id']]['fitness']
        if not (now_ms - boost_since >= MIN_TENURE and ft > max(fh * SWITCH_MARGIN, fh + .01)):
            top = hold
    if top and (not hold or top['id'] != hold['id']):
        boost_since = now_ms
    state['leader'], state['boost_since'] = (top['id'] if top else None), (boost_since if top else None)
    return changes


def prune(state):
    """Keep recent generations, the living population, the hall of fame and the team's ancestry."""
    nodes, gen = state['nodes'], state['generation']
    keep = set(state['population']) | set(state['hall'])
    seen, stack = set(), [m['id'] for m in state.get('team', [])] + ([state['champion']] if state['champion'] else [])
    while stack:                       # the team and all its ancestors
        nid = stack.pop()
        if nid in seen or nid not in nodes:
            continue
        seen.add(nid); stack += nodes[nid]['parents']
    keep |= seen
    keep |= {nid for nid, n in nodes.items() if n['born'] > gen - KEEP_GENERATIONS}
    state['nodes'] = {nid: n for nid, n in nodes.items() if nid in keep}
